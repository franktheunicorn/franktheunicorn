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
"""

from __future__ import annotations

import contextlib
import email.utils
import imaplib
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from franktheunicorn.data_access.email_inbox.parser import parse_email_message
from franktheunicorn.data_access.email_inbox.types import EmailFetchResult, InboxMessage

if TYPE_CHECKING:
    from franktheunicorn.config.models import SecurityEmailConfig

logger = logging.getLogger(__name__)

# Bound work per poll so a large mailbox can't stall the worker.
MAX_MESSAGES_PER_POLL = 50

# Header-only peek: enough to compute the message-id for dedup cheaply,
# before deciding whether to pull the full body.
_HEADER_PEEK = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
_FULL_PEEK = "(BODY.PEEK[])"


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
