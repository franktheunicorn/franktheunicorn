"""IMAP inbox poller for security report ingestion.

Connects to a configured IMAP inbox, fetches unread messages,
parses them, and returns InboxMessage instances for security reports.
"""

from __future__ import annotations

import contextlib
import imaplib
import logging
from typing import TYPE_CHECKING

from franktheunicorn.data_access.email_inbox.parser import parse_email_message
from franktheunicorn.data_access.email_inbox.types import InboxMessage

if TYPE_CHECKING:
    from franktheunicorn.config.models import SecurityEmailConfig

logger = logging.getLogger(__name__)


def fetch_security_emails(config: SecurityEmailConfig) -> list[InboxMessage]:
    """Poll IMAP inbox for unread messages that look like security reports.

    Connects, fetches UNSEEN messages, parses them, marks as read,
    and returns only those classified as security reports.
    """
    if not config.enabled:
        return []

    if not config.imap_host or not config.imap_user:
        logger.warning("Security email config incomplete (missing host or user).")
        return []

    try:
        conn = _connect(config)
    except Exception:
        logger.exception("Failed to connect to IMAP server %s", config.imap_host)
        return []

    messages: list[InboxMessage] = []
    id_count = 0
    try:
        conn.select(config.folder)
        _, message_ids = conn.search(None, "UNSEEN")
        id_list = message_ids[0].split() if message_ids[0] else []
        id_count = len(id_list)

        logger.info("Found %d unread messages in %s", id_count, config.folder)

        for msg_id in id_list:
            try:
                _, msg_data = conn.fetch(msg_id, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                raw_email = msg_data[0]
                if not isinstance(raw_email, tuple) or len(raw_email) < 2:
                    continue

                raw_bytes = raw_email[1]
                # raw_bytes may be bytes or str depending on IMAP server.
                byte_data = raw_bytes if isinstance(raw_bytes, bytes) else str(raw_bytes).encode()

                try:
                    parsed = parse_email_message(byte_data)
                except Exception:
                    logger.exception("Failed to parse email %s", msg_id)
                    parsed = None
                finally:
                    # Always mark fetched messages read — a poison message
                    # (bad Date header, unknown charset) that stayed UNSEEN
                    # would be re-fetched and re-fail every poll forever. The
                    # message itself remains in the mailbox for manual review.
                    conn.store(msg_id, "+FLAGS", "\\Seen")
                if parsed is not None and parsed.is_security_report:
                    messages.append(parsed)
            except Exception:
                logger.exception("Failed to fetch email %s", msg_id)
    except Exception:
        logger.exception("Error fetching from IMAP server")
    finally:
        with contextlib.suppress(Exception):
            conn.logout()

    logger.info(
        "Fetched %d security reports from %d total unread",
        len(messages),
        id_count,
    )
    return messages


def _connect(config: SecurityEmailConfig) -> imaplib.IMAP4_SSL | imaplib.IMAP4:
    """Establish IMAP connection."""
    conn: imaplib.IMAP4_SSL | imaplib.IMAP4
    if config.use_ssl:
        conn = imaplib.IMAP4_SSL(config.imap_host, config.imap_port)
    else:
        conn = imaplib.IMAP4(config.imap_host, config.imap_port)

    conn.login(config.imap_user, config.imap_pass)
    return conn
