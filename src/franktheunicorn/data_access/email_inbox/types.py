"""Data types for email inbox ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class InboxMessage:
    """A single email message fetched from an IMAP inbox."""

    message_id: str = ""
    subject: str = ""
    from_name: str = ""
    from_email: str = ""
    body: str = ""
    received_at: datetime | None = None
    is_security_report: bool = False
