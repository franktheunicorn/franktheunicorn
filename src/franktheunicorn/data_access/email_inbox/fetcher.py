"""Read-only IMAP inbox poller for security report ingestion.

This fetcher is deliberately **read-only and send-free**:

- The mailbox is selected ``readonly=True`` and messages are pulled with
  ``BODY.PEEK[...]`` so we never set the ``\\Seen`` flag — connecting the
  tool to a personal Gmail account must not silently mark mail as read.
- Nothing is ever sent. There is no SMTP path here.
- Dedup across polls is done against message-ids the *caller* has already
  recorded (in ``EmailScanRecord`` / ``SecurityReport``), not by mutating
  the mailbox.

The caller gets back every message examined this poll (via
``EmailFetchResult``) so it can write a visible audit row for each one.

The one deliberate, opt-in exception to "never mutate the mailbox" is
:func:`tag_ingested_messages`: when the operator sets ``ingested_tag`` in
config, messages that were actually ingested as security reports get that
tag added in the mailbox — as a Gmail label (``X-GM-LABELS``) when the
server supports Gmail's IMAP extensions, else as a standard IMAP keyword.
It adds exactly that one tag and nothing more: ``\\Seen`` is still never
set, and nothing is moved, deleted, or sent.
"""

from __future__ import annotations

import contextlib
import email.utils
import imaplib
import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from franktheunicorn.data_access.email_inbox.parser import parse_email_message
from franktheunicorn.data_access.email_inbox.types import (
    EmailFetchResult,
    EmailTagResult,
    InboxMessage,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import SecurityEmailConfig

logger = logging.getLogger(__name__)

# Bound work per poll so a large mailbox can't stall the worker.
MAX_MESSAGES_PER_POLL = 50

# Header-only peek: enough to compute the message-id for dedup cheaply,
# before deciding whether to pull the full body.
_HEADER_PEEK = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
_FULL_PEEK = "(BODY.PEEK[])"

# Characters that can't appear in an IMAP atom (RFC 3501 atom-specials plus
# controls); replaced with "_" when the tag is stored as a plain keyword.
_ATOM_UNSAFE = re.compile(r'[(){%*"\\\[\]\s\x00-\x1f\x7f]')


def fetch_security_emails(
    config: SecurityEmailConfig,
    already_seen_message_ids: Iterable[str] = (),
) -> EmailFetchResult:
    """Poll an IMAP inbox (read-only) and return every message examined.

    ``already_seen_message_ids`` are message-ids the caller has already
    ingested/recorded; those are skipped without pulling their bodies. The
    returned :class:`EmailFetchResult` lists every *new* message read this
    poll, each classified (security or not) with the keywords that matched.
    """
    result = EmailFetchResult()

    if not config.enabled:
        return result
    if not config.imap_host or not config.imap_user:
        logger.warning("Security email config incomplete (missing host or user).")
        result.error = "incomplete config"
        return result

    seen = {_normalize_mid(m) for m in already_seen_message_ids if m}

    try:
        conn = _connect(config)
    except Exception:
        logger.exception("Failed to connect to IMAP server %s", config.imap_host)
        result.error = "connection failed"
        return result

    try:
        # readonly=True: never modify the operator's mailbox.
        conn.select(config.folder, readonly=True)
        status, message_ids = conn.search(None, "UNSEEN")
        id_list = (
            message_ids[0].split() if (status == "OK" and message_ids and message_ids[0]) else []
        )
        result.unread_total = len(id_list)
        logger.info(
            "[email-scan] %d unread message(s) in %s (read-only; not marking seen)",
            result.unread_total,
            config.folder,
        )

        # Newest first, capped.
        for raw_id in reversed(id_list[-MAX_MESSAGES_PER_POLL:]):
            msg_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            try:
                mid = _peek_message_id(conn, msg_id)
                if mid and _normalize_mid(mid) in seen:
                    result.already_scanned += 1
                    continue
                parsed = _peek_full(conn, msg_id)
            except Exception:
                logger.exception("[email-scan] failed reading message %r", msg_id)
                continue
            if parsed is not None:
                result.examined.append(parsed)
                logger.info(
                    "[email-scan] read message from=%r subject=%r -> %s (keywords: %s)",
                    parsed.from_email or parsed.from_name or "unknown",
                    parsed.subject[:80],
                    "SECURITY" if parsed.is_security_report else "not security",
                    ", ".join(parsed.matched_keywords) or "none",
                )
    except Exception:
        logger.exception("[email-scan] error scanning IMAP folder")
        result.error = "scan failed"
    finally:
        with contextlib.suppress(Exception):
            conn.logout()

    logger.info(
        "[email-scan] examined %d new message(s); %d already scanned; %d classified as security",
        len(result.examined),
        result.already_scanned,
        sum(1 for m in result.examined if m.is_security_report),
    )
    return result


def tag_ingested_messages(
    config: SecurityEmailConfig,
    message_ids: Iterable[str],
) -> EmailTagResult:
    """Add ``config.ingested_tag`` to the given messages in the mailbox.

    This is the one deliberate mailbox write in the email path, and it only
    runs when the operator opts in by setting ``ingested_tag``. The tag is
    applied as a Gmail label (``X-GM-LABELS``) when the server advertises
    Gmail's IMAP extensions, otherwise as a standard IMAP keyword. Nothing
    else is touched: ``\\Seen`` is never set, and nothing is moved, deleted,
    or sent.

    Best-effort by design: by the time this runs the reports are already
    ingested, so failures are counted on the returned
    :class:`EmailTagResult` and logged — never raised.
    """
    result = EmailTagResult()
    tag = config.ingested_tag.strip()
    ids = [m.strip() for m in message_ids if m and m.strip()]
    if not tag or not ids:
        return result
    if not config.imap_host or not config.imap_user:
        logger.warning("[email-tag] security email config incomplete; cannot tag.")
        result.error = "incomplete config"
        return result

    try:
        conn = _connect(config)
    except Exception:
        logger.exception("[email-tag] failed to connect to IMAP server %s", config.imap_host)
        result.error = "connection failed"
        return result

    mode = "imap-keyword"
    try:
        # Read-write select — STORE requires it. Selecting read-write mutates
        # nothing by itself; the only write issued below is adding the tag.
        status, _ = conn.select(config.folder)
        if status != "OK":
            logger.warning("[email-tag] cannot select folder %r for tagging", config.folder)
            result.error = "select failed"
            return result

        use_gmail_labels = _supports_gmail_labels(conn)
        mode = "gmail-label" if use_gmail_labels else "imap-keyword"
        keyword = _ATOM_UNSAFE.sub("_", tag)

        for mid in ids:
            if not _searchable_mid(mid):
                logger.warning("[email-tag] skipping unsearchable message-id %r", mid[:120])
                result.skipped += 1
                continue
            try:
                status, data = conn.search(None, "HEADER", "Message-ID", f'"{mid}"')
                seqs = data[0].split() if (status == "OK" and data and data[0]) else []
                if not seqs:
                    logger.info(
                        "[email-tag] message %r not found in %s; cannot tag", mid, config.folder
                    )
                    result.missing += 1
                    continue
                seq_set = ",".join(s.decode() if isinstance(s, bytes) else str(s) for s in seqs)
                if use_gmail_labels:
                    status, _ = conn.store(seq_set, "+X-GM-LABELS.SILENT", f'("{tag}")')
                else:
                    status, _ = conn.store(seq_set, "+FLAGS.SILENT", f"({keyword})")
            except Exception:
                logger.exception("[email-tag] failed tagging message %r", mid)
                result.failed += 1
                continue
            if status == "OK":
                result.tagged += 1
            else:
                result.failed += 1
    except Exception:
        logger.exception("[email-tag] error tagging ingested messages")
        result.error = "tagging failed"
    finally:
        with contextlib.suppress(Exception):
            conn.logout()

    logger.info(
        "[email-tag] applied %r (%s): %d tagged, %d missing, %d failed, %d skipped",
        tag,
        mode,
        result.tagged,
        result.missing,
        result.failed,
        result.skipped,
    )
    return result


def _supports_gmail_labels(conn: imaplib.IMAP4 | imaplib.IMAP4_SSL) -> bool:
    """True when the server advertises Gmail's IMAP extensions (X-GM-EXT-1).

    Gmail only advertises this after authentication, and imaplib caches the
    pre-auth capability list, so ask the server again instead of trusting
    ``conn.capabilities``.
    """
    try:
        status, data = conn.capability()
    except Exception:
        logger.debug("[email-tag] capability probe failed", exc_info=True)
        return False
    if status != "OK" or not data or not data[0]:
        return False
    caps = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
    return b"X-GM-EXT-1" in caps.upper()


def _searchable_mid(mid: str) -> bool:
    """Only search message-ids we can safely embed in a quoted IMAP string."""
    return bool(mid) and all("!" <= ch <= "~" and ch not in '"\\' for ch in mid)


def _peek_message_id(conn: imaplib.IMAP4 | imaplib.IMAP4_SSL, msg_id: str) -> str:
    """Cheaply peek just the Message-ID header (does not set \\Seen)."""
    status, data = conn.fetch(msg_id, _HEADER_PEEK)
    if status != "OK" or not data:
        return ""
    raw = data[0]
    if not isinstance(raw, tuple) or len(raw) < 2:
        return ""
    header_bytes = raw[1] if isinstance(raw[1], bytes) else str(raw[1]).encode()
    text = header_bytes.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.lower().startswith("message-id:"):
            return line.split(":", 1)[1].strip()
    return ""


def _peek_full(conn: imaplib.IMAP4 | imaplib.IMAP4_SSL, msg_id: str) -> InboxMessage | None:
    """Peek the full message and parse it (does not set \\Seen)."""
    status, msg_data = conn.fetch(msg_id, _FULL_PEEK)
    if status != "OK" or not msg_data or not msg_data[0]:
        return None
    raw_email = msg_data[0]
    if not isinstance(raw_email, tuple) or len(raw_email) < 2:
        return None
    raw_bytes = raw_email[1]
    byte_data = raw_bytes if isinstance(raw_bytes, bytes) else str(raw_bytes).encode()
    return parse_email_message(byte_data)


def _normalize_mid(message_id: str) -> str:
    """Normalize a Message-ID for stable comparison."""
    return message_id.strip().strip("<>").lower()


def _connect(config: SecurityEmailConfig) -> imaplib.IMAP4_SSL | imaplib.IMAP4:
    """Establish an IMAP connection with a bounded timeout."""
    timeout = float(getattr(config, "timeout_seconds", 0) or 30)
    conn: imaplib.IMAP4_SSL | imaplib.IMAP4
    if config.use_ssl:
        conn = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=timeout)
    else:
        conn = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=timeout)
    conn.login(config.imap_user, config.imap_pass)
    return conn


# Kept importable for callers that still want raw address parsing.
parse_addr = email.utils.parseaddr
