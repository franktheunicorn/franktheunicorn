"""Email message parsing for security report ingestion.

Handles plain reports and *forwarded* reports. Security disclosures often
arrive as a forward — e.g. the Apache Security Team forwards a reporter's
message to the project PMC. In that case the envelope sender is the
forwarder, but the actual report (subject, reporter, body) lives inside the
forwarded block. ``parse_email_message`` unwraps that so downstream triage
sees the real report.
"""

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
        "arbitrary file",
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

# Matches the Gmail/Apple/Outlook "forwarded message" separators. The exact
# dash count and surrounding text vary by client, so match loosely.
_FORWARD_MARKER_RE = re.compile(
    r"^-{2,}\s*(?:forwarded message|original message)\s*-{2,}\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# A leading "Fwd:"/"Re:" (possibly repeated) on a subject line.
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:fwd?|re|aw|wg)\s*:\s*)+", re.IGNORECASE)

# Mailing-list "Name via listname" sender decoration.
_VIA_SUFFIX_RE = re.compile(r"\s+via\s+\S.*$", re.IGNORECASE)


def parse_email_message(raw_bytes: bytes) -> InboxMessage:
    """Parse raw email bytes into an InboxMessage.

    Unwraps a forwarded report so ``subject``/``from_*`` describe the
    original report rather than the forwarder's envelope. The full text
    (including the forward chain) is always kept in ``body`` so triage has
    complete context.
    """
    msg = email.message_from_bytes(raw_bytes)

    message_id = msg.get("Message-ID", "")
    envelope_subject = _decode_header_value(msg.get("Subject", ""))
    envelope_from_name, envelope_from_email = _parse_from(msg.get("From", ""))
    body = _extract_body(msg)
    received_at = _parse_date(msg.get("Date", ""))

    # Unwrap a forward, if present, to recover the original report metadata.
    unwrapped = _unwrap_forwarded(body)
    is_forwarded = unwrapped is not None
    if unwrapped is not None:
        subject = _strip_subject_prefixes(unwrapped.get("subject") or envelope_subject)
        inner_name, inner_email = _split_addr(unwrapped.get("from", ""))
        from_name = _clean_sender_name(inner_name) or envelope_from_name
        from_email = inner_email or envelope_from_email
    else:
        subject = _strip_subject_prefixes(envelope_subject)
        from_name = envelope_from_name
        from_email = envelope_from_email

    is_security, matched = _classify_security(subject, body)

    return InboxMessage(
        message_id=message_id,
        subject=subject,
        from_name=from_name,
        from_email=from_email,
        body=body,
        received_at=received_at,
        is_security_report=is_security,
        is_forwarded=is_forwarded,
        matched_keywords=matched,
        envelope_from_email=envelope_from_email,
    )


def _unwrap_forwarded(body: str) -> dict[str, str] | None:
    """Extract the innermost forwarded message's headers from a body.

    Returns a dict with any of ``from``/``subject``/``date`` found in the
    header block that follows the last forward marker, or None if the body
    isn't a forward. Uses the *last* marker so a report forwarded through
    several hops (reporter → security@ → PMC) resolves to the original.
    """
    markers = list(_FORWARD_MARKER_RE.finditer(body))
    if not markers:
        return None

    # Header block starts right after the last marker line.
    header_region = body[markers[-1].end() :]
    headers: dict[str, str] = {}
    for line in header_region.splitlines():
        stripped = line.strip()
        if not stripped:
            # Blank line ends the inline header block; the body follows.
            if headers:
                break
            continue
        m = re.match(r"^([A-Za-z-]+):\s*(.*)$", stripped)
        if not m:
            # A non-header line before we've seen any header — not a real
            # forward header block.
            if not headers:
                return None
            break
        key = m.group(1).lower()
        if key in ("from", "subject", "date", "to", "sent"):
            headers[key] = m.group(2).strip()

    if not any(k in headers for k in ("from", "subject")):
        return None
    return headers


def _classify_security(subject: str, body: str) -> tuple[bool, tuple[str, ...]]:
    """Return ``(is_security, matched_keywords)`` for transparency.

    A message is treated as a security report when at least two distinct
    security keywords appear across its subject and body. The matched terms
    are returned so the operator can see exactly why the filter decided as
    it did.
    """
    text = f"{subject} {body}".lower()
    matched = tuple(sorted(kw for kw in _SECURITY_KEYWORDS if kw in text))
    return (len(matched) >= 2, matched)


def _strip_subject_prefixes(subject: str) -> str:
    """Remove leading Fwd:/Re: decorations from a subject line."""
    return _SUBJECT_PREFIX_RE.sub("", subject).strip()


def _split_addr(from_value: str) -> tuple[str, str]:
    """Split an inline forwarded ``From:`` value into (name, email)."""
    if not from_value:
        return "", ""
    name, addr = email.utils.parseaddr(from_value)
    return _decode_header_value(name), addr


def _clean_sender_name(name: str) -> str:
    """Strip mailing-list ``via <list>`` decoration from a sender name."""
    return _VIA_SUFFIX_RE.sub("", name).strip()


def _decode_header_value(value: str) -> str:
    """Decode an email header that may have encoded words."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded: list[str] = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(_safe_decode(data, charset))
        else:
            decoded.append(data)
    return " ".join(decoded).strip()


def _parse_from(from_header: str) -> tuple[str, str]:
    """Parse From header into (name, email)."""
    if not from_header:
        return "", ""
    name, addr = email.utils.parseaddr(from_header)
    return _decode_header_value(name), addr


def _safe_decode(data: bytes, charset: str | None) -> str:
    """Decode bytes, tolerating unknown charset names.

    ``errors="replace"`` does not protect against a bogus charset *name* —
    ``bytes.decode("x-unknown")`` raises LookupError before error handling.
    """
    try:
        return data.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return data.decode("latin-1", errors="replace")


def _extract_body(msg: Message) -> str:
    """Extract the plain-text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return _safe_decode(payload, part.get_content_charset())
        # Fallback: try HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return _strip_html(_safe_decode(payload, part.get_content_charset()))
        return ""

    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return _safe_decode(payload, msg.get_content_charset())
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
