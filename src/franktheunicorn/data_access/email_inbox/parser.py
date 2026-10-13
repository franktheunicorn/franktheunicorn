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
import hashlib
import logging
import re
from datetime import UTC, datetime
from email.header import decode_header
from email.message import Message

from franktheunicorn.data_access.email_inbox.types import AttachmentMeta, InboxMessage

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

# Relays frank recognises. On an ASF project most reports arrive through the
# Security Team's forwarding template, whose From: is the broker and whose
# last line credits the discoverer. The ASF adapter below is Apache Magpie's
# (``tools/forwarder-relay/README.md``, the ``asf-security`` entry, at
# apache/magpie commit dadb315caa8b881fc1d2195bef03223dac25915a, Apache-2.0),
# with two changes: the preamble decides on its own, because a pasted report
# has no envelope to match the sender pattern against; and the credit regex
# runs to the end of the line instead of stopping at the first period, because
# Magpie's ``(?P<credit>.+?)\.`` turns "bugbunny.ai" into "bugbunny".


def _wrap_tolerant(phrase: str) -> str:
    """*phrase* as a regex that survives a mail client's line wrapping.

    Word by word with ``\\s+`` between, not Magpie's literal spaces: clients
    wrap the template at 72 columns wherever the line happens to end.
    """
    return r"\s+".join(re.escape(word) for word in phrase.split())


_ASF_PREAMBLE_RE = re.compile(
    _wrap_tolerant(
        "Dear PMC, The security vulnerability report has been received by the Apache Security Team"
    ),
    re.IGNORECASE,
)
_ASF_CREDIT_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        _wrap_tolerant("This vulnerability was discovered and reported by")
        + r"\s+(?P<credit>[^\n]+?)\.?[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"^Credit:[ \t]+(?P<credit>[^\n]+?)\.?[ \t]*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Reported by:[ \t]+(?P<credit>[^\n]+?)\.?[ \t]*$", re.IGNORECASE | re.MULTILINE),
)
# Magpie's kind hints, in its order (tool before service); the default is human.
# A tool is credited in a CVE 5 record as type "tool", not as a finder.
_CREDIT_KIND_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tool", re.compile(r"\.ai\b|bot\b|scanner\b", re.IGNORECASE)),
    ("service", re.compile(r"security team\b|\bsoc\b", re.IGNORECASE)),
)
# A GitHub Security Advisory notification: GitHub's sender plus the advisory
# id in the subject. Not Magpie's; its GHSA path reads the advisory via the
# API, which frank does not do here.
_GHSA_SENDER_RE = re.compile(r"@github\.com$", re.IGNORECASE)
_GHSA_ID_RE = re.compile(r"\bGHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}\b", re.IGNORECASE)

#: Enough attachments to describe any real report; past this it is a dump.
_MAX_ATTACHMENTS = 50


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
    relay_kind, reporter_credit, credit_kind = _detect_relay(
        envelope_from_email, envelope_subject, body
    )

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
        relay_kind=relay_kind,
        reporter_credit=reporter_credit,
        credit_kind=credit_kind,
        attachments=_attachments(msg),
    )


def parse_pasted_report(text: str) -> InboxMessage:
    """Parse a security report pasted directly into the dashboard.

    The paste path is the "not ready to connect an inbox yet" fallback: the
    operator copies a report — very often a *forwarded* Apache Security Team
    email — into a textarea. This mirrors :func:`parse_email_message` but
    operates on the pasted text as the body (there is no MIME envelope to
    decode). It unwraps a forwarded block so ``subject``/``from_*`` describe
    the original disclosure rather than the forwarding boilerplate, which
    means the reporter and title are recovered even when no LLM backend is
    configured to parse the report. Operator-entered values always take
    precedence over what this recovers; it only fills the blanks.
    """
    body = text or ""

    unwrapped = _unwrap_forwarded(body)
    is_forwarded = unwrapped is not None
    if unwrapped is not None:
        subject = _strip_subject_prefixes(unwrapped.get("subject", ""))
        inner_name, inner_email = _split_addr(unwrapped.get("from", ""))
        from_name = _clean_sender_name(inner_name)
        from_email = inner_email
    else:
        subject = ""
        from_name = ""
        from_email = ""

    is_security, matched = _classify_security(subject, body)
    # No envelope, so only the preamble-detected relay can match here.
    relay_kind, reporter_credit, credit_kind = _detect_relay("", subject, body)

    # Pasted reports carry no reliable received timestamp (any date lives
    # inside the forwarded block in client-specific formatting), so
    # ``received_at`` is intentionally left unset.
    return InboxMessage(
        subject=subject,
        from_name=from_name,
        from_email=from_email,
        body=body,
        is_security_report=is_security,
        is_forwarded=is_forwarded,
        matched_keywords=matched,
        relay_kind=relay_kind,
        reporter_credit=reporter_credit,
        credit_kind=credit_kind,
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


def _credit_kind(credit: str) -> str:
    for kind, pattern in _CREDIT_KIND_HINTS:
        if pattern.search(credit):
            return kind
    return "human"


def _detect_relay(envelope_from_email: str, subject: str, body: str) -> tuple[str, str, str]:
    """``(relay_kind, reporter_credit, credit_kind)``; all empty when not a relay.

    The ASF relay is its preamble: the sender pattern Magpie also checks
    would confirm it but cannot decide it, since a pasted report has no
    envelope. The GHSA relay needs both GitHub's sender and the advisory id
    in the subject; neither alone means anything.
    """
    if _ASF_PREAMBLE_RE.search(body):
        for pattern in _ASF_CREDIT_RES:
            m = pattern.search(body)
            if m:
                credit = m.group("credit").strip()[:255]
                return "asf-security", credit, _credit_kind(credit)
        return "asf-security", "", ""
    if (
        envelope_from_email
        and _GHSA_SENDER_RE.search(envelope_from_email)
        and _GHSA_ID_RE.search(subject)
    ):
        return "ghsa", "", ""
    return "", "", ""


def _attachments(msg: Message) -> tuple[AttachmentMeta, ...]:
    """Name, declared type, size and sha256 of every attachment; bytes dropped.

    An attachment is a part with a filename. The content type is what the
    sender declared, which is data like the rest: a ``text/plain`` label on a
    zip is a claim, and the sha256 is there so the operator can tell two
    reports with the same ``poc.zip`` apart.
    """
    if not msg.is_multipart():
        return ()
    found: list[AttachmentMeta] = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        found.append(
            AttachmentMeta(
                filename=_decode_header_value(filename)[:255],
                content_type=part.get_content_type(),
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
        if len(found) >= _MAX_ATTACHMENTS:
            break
    return tuple(found)


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
        # A ``poc.txt`` attachment is text/plain too; without the disposition
        # check it became the body of an HTML-only mail and the real text was
        # dropped. It is in the attachment manifest instead.
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain" and part.get_content_disposition() != "attachment":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return _safe_decode(payload, part.get_content_charset())
        # Fallback: try HTML
        for part in msg.walk():
            if (
                part.get_content_type() == "text/html"
                and part.get_content_disposition() != "attachment"
            ):
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
