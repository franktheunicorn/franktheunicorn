"""IMAP fetcher for private/authenticated mailing lists (v1.5).

Some mailing lists (e.g. Apache ``private@`` lists) aren't reachable through the
public lists.apache.org archive/API. When a mailing-list community source sets
``imap_host``, the context orchestrator uses this fetcher instead of the archive
scrape/API path: it connects over IMAP, searches the configured folder by
subject, and returns the same :class:`MailingListSearchResult` the archive
fetcher produces.

Off by default — only used when ``imap_host`` is configured. Degrades
gracefully (returns an empty result, never raises) when the config is
incomplete or the server is unreachable, so the surrounding context pipeline
keeps working.
"""

from __future__ import annotations

import contextlib
import email
import email.utils
import imaplib
import logging
from email.header import decode_header
from email.message import Message
from typing import TYPE_CHECKING

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.mailing_list.fetcher import _extract_pr_references
from franktheunicorn.data_access.mailing_list.types import (
    MailingListSearchResult,
    MailingListThread,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import CommunitySourceConfig

logger = logging.getLogger(__name__)

# Cap how many matching messages we pull per query to keep latency bounded;
# mirrors the 20-thread cap the archive cache uses.
MAX_MESSAGES = 20

# Either IMAP connection flavor (SSL or plaintext).
ImapConn = imaplib.IMAP4_SSL | imaplib.IMAP4


def fetch_mailing_list_imap(
    config: CommunitySourceConfig,
    query: str,
    blame_hit: bool = False,
) -> MailingListSearchResult:
    """Search a mailing list over IMAP and return matching threads.

    Connects to ``config.imap_host``, selects ``config.imap_folder``, searches
    for messages whose subject contains ``query``, and parses them into
    :class:`MailingListThread` objects.
    """
    list_name = config.name or config.imap_folder
    empty = MailingListSearchResult(
        fetched_via=FetchMethod.API, threads=[], query=query, list_name=list_name
    )

    if not config.imap_host or not config.imap_user:
        logger.warning("Mailing list IMAP config incomplete (missing host or user).")
        return empty

    try:
        conn = _connect(config)
    except Exception:
        logger.exception("Failed to connect to IMAP server %s", config.imap_host)
        return empty

    threads: list[MailingListThread] = []
    try:
        # readonly: searching must not set \Seen flags on the operator's
        # private mailbox (a plain SELECT + FETCH RFC822 marks messages read).
        conn.select(config.imap_folder, readonly=True)
        try:
            criterion = _quote_search_term(query)
        except UnicodeEncodeError:
            # IMAP commands are ASCII; skip queries we can't encode rather
            # than erroring the whole context pipeline.
            logger.debug("Skipping non-ASCII IMAP query %r", query)
            return empty
        status, data = conn.search(None, "SUBJECT", criterion)
        if status != "OK" or not data or not data[0]:
            return empty
        # SEARCH returns ids in ascending (oldest-first) order — keep the
        # newest matches, not the oldest.
        for msg_id in data[0].split()[-MAX_MESSAGES:]:
            # One malformed message must not discard the threads already
            # parsed — degrade per message, not per query.
            try:
                raw = _fetch_rfc822(conn, msg_id)
                if raw is None:
                    continue
                thread = _parse_message(raw, query, list_name, blame_hit=blame_hit)
            except Exception:
                logger.debug("Skipping unparseable IMAP message", exc_info=True)
                continue
            if thread is not None:
                threads.append(thread)
    except Exception:
        logger.exception("Error searching mailing list over IMAP")
        return empty
    finally:
        with contextlib.suppress(Exception):
            conn.logout()

    return MailingListSearchResult(
        fetched_via=FetchMethod.API,
        threads=threads,
        query=query,
        list_name=list_name,
    )


def _quote_search_term(term: str) -> str:
    """RFC 3501-quote a SEARCH criterion value.

    imaplib joins criteria with spaces and does no quoting itself, so an
    unquoted multi-word query ("Fix executor memory leak") is an invalid
    SEARCH command. Raises ``UnicodeEncodeError`` for non-ASCII terms, which
    the caller treats as "skip this query".
    """
    term.encode("ascii")  # IMAP commands are ASCII — probe before quoting.
    escaped = term.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _connect(config: CommunitySourceConfig) -> ImapConn:
    """Open and authenticate an IMAP connection.

    Applies the source's ``timeout_seconds`` so a hung server can't block
    the worker's poll cycle indefinitely.
    """
    timeout = float(config.timeout_seconds or 30)
    conn: ImapConn
    if config.use_ssl:
        conn = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=timeout)
    else:
        conn = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=timeout)
    conn.login(config.imap_user, config.imap_pass)
    return conn


def _fetch_rfc822(conn: ImapConn, msg_id: object) -> bytes | None:
    """Fetch one message's raw RFC822 bytes, or ``None`` if unavailable.

    ``msg_id`` comes straight out of an IMAP ``SEARCH`` response; imaplib accepts
    either ``str`` or ``bytes`` ids, so it's left loosely typed here.
    """
    status, msg_data = conn.fetch(msg_id, "(RFC822)")  # type: ignore[arg-type]
    if status != "OK" or not msg_data:
        return None
    raw = msg_data[0]
    if not isinstance(raw, tuple) or len(raw) < 2:
        return None
    body = raw[1]
    return body if isinstance(body, bytes) else str(body).encode()


def _parse_message(
    raw: bytes,
    query: str,
    list_name: str,
    *,
    blame_hit: bool,
) -> MailingListThread | None:
    """Parse raw RFC822 bytes into a thread, or ``None`` to skip a non-match."""
    msg = email.message_from_bytes(raw)
    subject = _decode_header_value(msg.get("Subject", ""))
    # Mirror the archive path: keep only case-insensitive subject substring matches.
    if query.lower() not in subject.lower():
        return None

    sender = _sender(msg.get("From", ""))
    snippet = _body_snippet(msg)
    return MailingListThread(
        fetched_via=FetchMethod.API,
        subject=subject,
        date=msg.get("Date", ""),
        participants=[sender] if sender else [],
        snippet=snippet,
        url=msg.get("Message-ID", ""),
        list_name=list_name,
        pr_references=_extract_pr_references(f"{subject} {snippet}"),
        blame_hit=blame_hit,
    )


def _safe_decode(data: bytes, charset: str | None) -> str:
    """Decode bytes, falling back to latin-1 for unknown charset names.

    ``errors="replace"`` does not guard against a bogus charset *name* —
    ``bytes.decode("x-unknown")`` raises ``LookupError`` before error
    handling applies.
    """
    try:
        return data.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return data.decode("latin-1", errors="replace")


def _decode_header_value(value: str) -> str:
    """Decode a possibly RFC 2047-encoded header value."""
    if not value:
        return ""
    decoded: list[str] = []
    for data, charset in decode_header(value):
        if isinstance(data, bytes):
            decoded.append(_safe_decode(data, charset))
        else:
            decoded.append(data)
    return " ".join(decoded).strip()


def _sender(from_header: str) -> str:
    """Return a display name (falling back to the address) for a From header."""
    if not from_header:
        return ""
    name, addr = email.utils.parseaddr(from_header)
    return _decode_header_value(name) or addr


def _body_snippet(msg: Message, limit: int = 500) -> str:
    """Extract a truncated plain-text body snippet from a message."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    text = _safe_decode(payload, part.get_content_charset())
                    break
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            text = _safe_decode(payload, msg.get_content_charset())
    return text[:limit]
