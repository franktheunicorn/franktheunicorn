"""Email message parsing for security report ingestion."""

from __future__ import annotations

import email
import email.utils
import logging
import re
from datetime import UTC, datetime
from email.header import decode_header
from email.message import Message

from franktheunicorn.data_access.email_inbox.types import InboxMessage

logger = logging.getLogger(__name__)

# Keywords that suggest an email is a security report.
_SECURITY_KEYWORDS: frozenset[str] = frozenset(
    {
        "vulnerability",
        "cve",
        "security",
        "exploit",
        "poc",
        "proof of concept",
        "proof-of-concept",
        "rce",
        "xss",
        "sqli",
        "sql injection",
        "buffer overflow",
        "heap overflow",
        "use after free",
        "use-after-free",
        "privilege escalation",
        "arbitrary code",
        "remote code execution",
        "denial of service",
        "dos",
        "path traversal",
        "directory traversal",
        "ssrf",
        "idor",
        "command injection",
        "deserialization",
        "bypass",
        "disclosure",
    }
)


def parse_email_message(raw_bytes: bytes) -> InboxMessage:
    """Parse raw email bytes into an InboxMessage."""
    msg = email.message_from_bytes(raw_bytes)

    message_id = msg.get("Message-ID", "")
    subject = _decode_header_value(msg.get("Subject", ""))
    from_name, from_email_addr = _parse_from(msg.get("From", ""))
    body = _extract_body(msg)
    received_at = _parse_date(msg.get("Date", ""))

    is_security = _is_security_report(subject, body)

    return InboxMessage(
        message_id=message_id,
        subject=subject,
        from_name=from_name,
        from_email=from_email_addr,
        body=body,
        received_at=received_at,
        is_security_report=is_security,
    )


def _decode_header_value(value: str) -> str:
    """Decode an email header that may have encoded words."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded: list[str] = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded).strip()


def _parse_from(from_header: str) -> tuple[str, str]:
    """Parse From header into (name, email)."""
    if not from_header:
        return "", ""
    name, addr = email.utils.parseaddr(from_header)
    return _decode_header_value(name), addr


def _extract_body(msg: Message) -> str:
    """Extract the plain-text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    return _strip_html(html)
        return ""

    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = msg.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            # errors="replace" doesn't guard against an unknown charset
            # *name* — decode raises LookupError before error handling.
            return payload.decode("latin-1", errors="replace")
    return str(payload) if payload else ""


def _strip_html(html: str) -> str:
    """Crude HTML tag stripping for fallback body extraction."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_date(date_str: str) -> datetime | None:
    """Parse an email date header into a datetime.

    Returns None for malformed headers — a report with a bad Date: must
    still be ingested, not error out of the pipeline.
    """
    if not date_str:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _is_security_report(subject: str, body: str) -> bool:
    """Heuristic check whether an email is likely a security report."""
    text = f"{subject} {body}".lower()
    matches = sum(1 for kw in _SECURITY_KEYWORDS if kw in text)
    return matches >= 2
