"""Data types for email inbox ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class AttachmentMeta:
    """What an attachment is, never what it contains.

    Recorded at intake as data (name, declared type, size, sha256) so a report
    whose PoC is a zip is not read as a report with no PoC. The bytes are not
    stored and nothing is extracted or executed; a PoC runs only through the
    click-gated container sandbox, on text the operator pasted into it. This is
    Apache Magpie's PoC handling policy (``docs/security/poc-handling-policy.md``
    at apache/magpie commit dadb315caa8b881fc1d2195bef03223dac25915a): fetch
    and display first, read statically, execute only in a container and only
    when static reading cannot answer, never binaries.
    """

    filename: str
    content_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class InboxMessage:
    """A single email message fetched from an IMAP inbox.

    ``from_name``/``from_email``/``subject`` reflect the *original* report
    when the email is a forward (e.g. an Apache Security Team forward wrapping
    a reporter's message): the envelope sender is the forwarder, but the
    report is what we care about. ``matched_keywords`` records exactly which
    security terms tripped the filter, so the classification is inspectable.

    ``relay_kind``/``reporter_credit``/``credit_kind`` are set when a known
    relay (the ASF Security Team, a GitHub Security Advisory) forwarded the
    report: the relay's credit line names the discoverer, and that name, not
    the sender, is what an advisory should credit.
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
    #: "asf-security", "ghsa", or "" when no known relay carried the report.
    relay_kind: str = ""
    #: The name the relay's credit line gives, verbatim; "" when it gave none.
    reporter_credit: str = ""
    #: "human", "tool" or "service" for a credited name; "" without one.
    credit_kind: str = ""
    #: Every MIME attachment, as metadata only. Empty for a pasted report.
    attachments: tuple[AttachmentMeta, ...] = ()

    def attachment_rows(self) -> list[dict[str, str | int]]:
        """The manifest as plain rows, the shape ``SecurityReport.attachments`` stores."""
        return [
            {
                "filename": a.filename,
                "content_type": a.content_type,
                "size_bytes": a.size_bytes,
                "sha256": a.sha256,
            }
            for a in self.attachments
        ]


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
