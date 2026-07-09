"""Data types for email inbox ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class InboxMessage:
    """A single email message fetched from an IMAP inbox.

    ``from_name``/``from_email``/``subject`` reflect the *original* report
    when the email is a forward (e.g. an Apache Security Team forward wrapping
    a reporter's message): the envelope sender is the forwarder, but the
    report is what we care about. ``matched_keywords`` records exactly which
    security terms tripped the filter, so the classification is inspectable.
    """

    message_id: str = ""
    subject: str = ""
    from_name: str = ""
    from_email: str = ""
    body: str = ""
    received_at: datetime | None = None
    is_security_report: bool = False
    is_forwarded: bool = False
    matched_keywords: tuple[str, ...] = ()
    # The outer envelope sender (the forwarder), kept for the audit trail.
    envelope_from_email: str = ""


@dataclass
class EmailFetchResult:
    """Everything a single read-only inbox poll examined.

    The fetcher opens the mailbox read-only (never marks messages seen, never
    sends), so the caller gets the full list of messages it looked at — not
    just the security ones — and can record an audit row for each.
    """

    examined: list[InboxMessage] = field(default_factory=list)
    unread_total: int = 0
    already_scanned: int = 0
    error: str = ""


@dataclass
class EmailTagResult:
    """Outcome of tagging ingested messages in the mailbox (opt-in).

    ``skipped`` counts messages we chose not to touch (no message-id, or an
    id that can't be searched safely); ``missing`` counts ids the server no
    longer found; ``failed`` counts STORE attempts the server rejected.
    """

    tagged: int = 0
    missing: int = 0
    failed: int = 0
    skipped: int = 0
    error: str = ""
