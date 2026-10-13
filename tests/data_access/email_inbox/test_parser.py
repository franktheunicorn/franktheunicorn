"""Tests for email message parsing."""

from __future__ import annotations

import email.mime.text
from email.mime.multipart import MIMEMultipart

from franktheunicorn.data_access.email_inbox.parser import (
    _classify_security,
    _strip_html,
    _unwrap_forwarded,
    parse_email_message,
    parse_pasted_report,
)


class TestParseEmailMessage:
    def test_parses_simple_text_email(self) -> None:
        msg = email.mime.text.MIMEText(
            "There is a vulnerability in the parser module.\nPOC: run `exploit.py` to trigger RCE."
        )
        msg["Subject"] = "Security vulnerability in parser"
        msg["From"] = "Alice Reporter <alice@example.com>"
        msg["Message-ID"] = "<test-123@example.com>"
        msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"

        result = parse_email_message(msg.as_bytes())

        assert result.subject == "Security vulnerability in parser"
        assert result.from_name == "Alice Reporter"
        assert result.from_email == "alice@example.com"
        assert result.message_id == "<test-123@example.com>"
        assert "vulnerability" in result.body
        assert result.is_security_report is True
        assert result.is_forwarded is False
        assert "vulnerability" in result.matched_keywords

    def test_parses_multipart_email(self) -> None:
        msg = MIMEMultipart()
        msg["Subject"] = "CVE report"
        msg["From"] = "reporter@example.com"
        msg["Message-ID"] = "<multi-123>"
        msg.attach(email.mime.text.MIMEText("SQL injection exploit found"))
        msg.attach(email.mime.text.MIMEText("<h1>HTML version</h1>", "html"))

        result = parse_email_message(msg.as_bytes())
        assert "SQL injection" in result.body
        assert result.is_security_report is True

    def test_non_security_email(self) -> None:
        msg = email.mime.text.MIMEText("Please review my PR for the new feature.")
        msg["Subject"] = "PR review request"
        msg["From"] = "dev@example.com"
        msg["Message-ID"] = "<nonsec-123>"

        result = parse_email_message(msg.as_bytes())
        assert result.is_security_report is False
        assert result.matched_keywords == ()

    def test_from_without_name(self) -> None:
        msg = email.mime.text.MIMEText("security vulnerability CVE report")
        msg["Subject"] = "Test"
        msg["From"] = "bare@example.com"
        msg["Message-ID"] = "<bare-123>"

        result = parse_email_message(msg.as_bytes())
        assert result.from_name == ""
        assert result.from_email == "bare@example.com"

    def test_strips_fwd_prefix_from_subject(self) -> None:
        msg = email.mime.text.MIMEText("just a plain note")
        msg["Subject"] = "Fwd: Re: Fwd: Important thing"
        msg["From"] = "a@example.com"
        result = parse_email_message(msg.as_bytes())
        assert result.subject == "Important thing"


# The exact shape of an Apache Security Team forward wrapping a reporter's
# disclosure, as in the operator's example.
_APACHE_FORWARD = """\
Dear PMC,

The security vulnerability report has been received by the Apache
Security Team and is being passed to you for action.

---------- Forwarded message ---------
From: Ryan Hughes via security <security@apache.org>
Date: Sun, Apr 19, 2026 at 8:31 PM
Subject: [SECURITY] Path traversal via core_model_path in PySpark ML pipeline metadata
To: <security@spark.apache.org>


Hello Apache Security / Spark PMC,

I am reporting a security issue in PySpark's ML pipeline metadata loader.

Summary
When loading a PySpark ML pipeline, the core_model_path field in the
pipeline's metadata JSON is consumed without path-traversal validation.

Impact
- Arbitrary file read on the Spark driver/worker.

Thanks,
Ryan Hughes
"""


class TestForwardedEmail:
    def test_unwrap_forwarded_extracts_inner_headers(self) -> None:
        headers = _unwrap_forwarded(_APACHE_FORWARD)
        assert headers is not None
        assert "Ryan Hughes via security" in headers["from"]
        assert headers["subject"].startswith("[SECURITY] Path traversal")

    def test_unwrap_returns_none_for_plain_body(self) -> None:
        assert _unwrap_forwarded("Just a normal email with no forward.") is None

    def test_parses_apache_security_forward(self) -> None:
        msg = email.mime.text.MIMEText(_APACHE_FORWARD)
        # Envelope is the Apache Security Team, not the reporter.
        msg["Subject"] = "Fwd: [SECURITY] Path traversal via core_model_path"
        msg["From"] = "Apache Security Team <security@apache.org>"
        msg["Message-ID"] = "<fwd-1@apache.org>"

        result = parse_email_message(msg.as_bytes())

        assert result.is_forwarded is True
        # Subject/reporter come from the *original* report, not the envelope.
        assert result.subject.startswith("[SECURITY] Path traversal")
        assert result.from_name == "Ryan Hughes"  # "via security" stripped
        assert result.envelope_from_email == "security@apache.org"
        # Full text (incl. the Apache boilerplate) is preserved for triage.
        assert "received by the Apache" in result.body
        assert "core_model_path" in result.body
        # Classified as security via path-traversal + vulnerability + security.
        assert result.is_security_report is True
        assert "path traversal" in result.matched_keywords

    def test_nested_forward_uses_innermost(self) -> None:
        body = (
            "outer note\n"
            "---------- Forwarded message ---------\n"
            "From: Middle Person <mid@example.com>\n"
            "Subject: Fwd: original\n"
            "\n"
            "some text\n"
            "---------- Forwarded message ---------\n"
            "From: Real Reporter <real@example.com>\n"
            "Subject: [SECURITY] the actual vulnerability with an exploit\n"
            "\n"
            "the real report body\n"
        )
        msg = email.mime.text.MIMEText(body)
        msg["Subject"] = "Fwd: Fwd: something"
        msg["From"] = "forwarder@example.com"
        result = parse_email_message(msg.as_bytes())
        assert result.from_email == "real@example.com"
        assert result.subject.startswith("[SECURITY] the actual vulnerability")


class TestParsePastedReport:
    """The paste path: the operator copies a report (often a forwarded email)
    into a textarea. Metadata must be recovered from the text itself, with no
    MIME envelope and no LLM backend required."""

    def test_plain_text_has_no_recovered_metadata(self) -> None:
        result = parse_pasted_report("A short vulnerability note about an exploit.")
        assert result.is_forwarded is False
        assert result.subject == ""
        assert result.from_name == ""
        assert result.from_email == ""
        # Body is preserved verbatim for triage.
        assert result.body == "A short vulnerability note about an exploit."

    def test_recovers_reporter_and_title_from_forward(self) -> None:
        result = parse_pasted_report(_APACHE_FORWARD)
        assert result.is_forwarded is True
        # "via security" decoration stripped from the reporter name.
        assert result.from_name == "Ryan Hughes"
        assert result.from_email == "security@apache.org"
        # [SECURITY] kept; no Fwd:/Re: to strip here.
        assert result.subject.startswith("[SECURITY] Path traversal")
        # Full pasted text — including the Apache boilerplate — kept for triage.
        assert "received by the Apache" in result.body
        assert "core_model_path" in result.body

    def test_classifies_security_keywords(self) -> None:
        result = parse_pasted_report(_APACHE_FORWARD)
        assert result.is_security_report is True
        assert "path traversal" in result.matched_keywords

    def test_empty_text_is_safe(self) -> None:
        result = parse_pasted_report("")
        assert result.body == ""
        assert result.is_forwarded is False
        assert result.is_security_report is False

    def test_uses_innermost_forward(self) -> None:
        pasted = (
            "PMC boilerplate\n"
            "---------- Forwarded message ---------\n"
            "From: Middle Person <mid@example.com>\n"
            "Subject: Fwd: original\n"
            "\n"
            "some text\n"
            "---------- Forwarded message ---------\n"
            "From: Real Reporter <real@example.com>\n"
            "Subject: [SECURITY] the actual vulnerability with an exploit\n"
            "\n"
            "the real report body\n"
        )
        result = parse_pasted_report(pasted)
        assert result.from_email == "real@example.com"
        assert result.subject.startswith("[SECURITY] the actual vulnerability")


class TestClassifySecurity:
    def test_requires_two_keywords(self) -> None:
        is_sec, matched = _classify_security("vulnerability", "exploit found")
        assert is_sec is True
        assert set(matched) >= {"vulnerability", "exploit"}

        is_sec, matched = _classify_security("hello", "world")
        assert is_sec is False
        assert matched == ()

    def test_single_keyword_not_enough(self) -> None:
        is_sec, matched = _classify_security("security", "please review")
        assert is_sec is False
        assert matched == ("security",)

    def test_multiple_keywords(self) -> None:
        is_sec, matched = _classify_security(
            "CVE-2024-1234 vulnerability",
            "proof of concept for RCE",
        )
        assert is_sec is True
        assert len(matched) >= 2


class TestStripHtml:
    def test_strips_tags(self) -> None:
        assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_handles_empty(self) -> None:
        assert _strip_html("") == ""


_ASF_CREDIT_LINE = "\nThis vulnerability was discovered and reported by bugbunny.ai.\n"


class TestRelayDetection:
    """The relay's sender is the broker; its credit line names the discoverer."""

    def test_asf_relay_by_preamble_with_a_tool_credit(self) -> None:
        msg = email.mime.text.MIMEText(_APACHE_FORWARD + _ASF_CREDIT_LINE)
        msg["Subject"] = "Fwd: [SECURITY] Path traversal"
        msg["From"] = "Apache Security Team <security@apache.org>"

        result = parse_email_message(msg.as_bytes())

        assert result.relay_kind == "asf-security"
        # Magpie's own regex stops at the first period and would say "bugbunny".
        assert result.reporter_credit == "bugbunny.ai"
        assert result.credit_kind == "tool"
        # The unwrapped reporter is untouched; the credit is a separate fact.
        assert result.from_name == "Ryan Hughes"

    def test_asf_relay_without_a_credit_line(self) -> None:
        msg = email.mime.text.MIMEText(_APACHE_FORWARD)
        msg["From"] = "security@apache.org"
        result = parse_email_message(msg.as_bytes())
        assert result.relay_kind == "asf-security"
        assert result.reporter_credit == ""
        assert result.credit_kind == ""

    def test_credit_and_reported_by_lines_and_their_kinds(self) -> None:
        for line, name, kind in (
            ("Credit: Jane Doe", "Jane Doe", "human"),
            ("Reported by: Acme Security Team.", "Acme Security Team", "service"),
            ("Reported by: nightly-scanner", "nightly-scanner", "tool"),
        ):
            result = parse_pasted_report(_APACHE_FORWARD + "\n" + line + "\n")
            assert result.relay_kind == "asf-security"
            assert result.reporter_credit == name, line
            assert result.credit_kind == kind, line

    def test_pasted_relay_is_detected_without_an_envelope(self) -> None:
        result = parse_pasted_report(_APACHE_FORWARD + _ASF_CREDIT_LINE)
        assert result.relay_kind == "asf-security"
        assert result.reporter_credit == "bugbunny.ai"

    def test_ghsa_needs_the_sender_and_the_advisory_id(self) -> None:
        msg = email.mime.text.MIMEText("A vulnerability with an exploit was reported.")
        msg["Subject"] = "[org/repo] Path traversal (GHSA-abcd-1234-wxyz)"
        msg["From"] = "GitHub <notifications@github.com>"
        assert parse_email_message(msg.as_bytes()).relay_kind == "ghsa"

        no_id = email.mime.text.MIMEText("A vulnerability with an exploit was reported.")
        no_id["Subject"] = "[org/repo] Path traversal"
        no_id["From"] = "GitHub <notifications@github.com>"
        assert parse_email_message(no_id.as_bytes()).relay_kind == ""

        wrong_sender = email.mime.text.MIMEText("A vulnerability with an exploit was reported.")
        wrong_sender["Subject"] = "Re: GHSA-abcd-1234-wxyz"
        wrong_sender["From"] = "mallory@example.com"
        assert parse_email_message(wrong_sender.as_bytes()).relay_kind == ""

    def test_a_direct_report_is_not_a_relay(self) -> None:
        msg = email.mime.text.MIMEText("There is a vulnerability; exploit attached.")
        msg["From"] = "alice@example.com"
        msg["Subject"] = "security issue"
        result = parse_email_message(msg.as_bytes())
        assert (result.relay_kind, result.reporter_credit, result.credit_kind) == ("", "", "")


class TestAttachments:
    """The manifest records what an attachment is, never what it contains."""

    def _mail_with(self, body: str, parts: list[tuple[str, str, bytes]]) -> bytes:
        from email.mime.application import MIMEApplication

        msg = MIMEMultipart()
        msg["Subject"] = "vulnerability with exploit"
        msg["From"] = "alice@example.com"
        msg.attach(email.mime.text.MIMEText(body))
        for filename, subtype, payload in parts:
            part = MIMEApplication(payload, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
        return msg.as_bytes()

    def test_manifest_has_name_type_size_and_digest(self) -> None:
        import hashlib

        payload = b"PK\x03\x04not really a zip"
        result = parse_email_message(self._mail_with("see the zip", [("poc.zip", "zip", payload)]))

        assert result.body.strip() == "see the zip"
        assert len(result.attachments) == 1
        meta = result.attachments[0]
        assert meta.filename == "poc.zip"
        assert meta.content_type == "application/zip"
        assert meta.size_bytes == len(payload)
        assert meta.sha256 == hashlib.sha256(payload).hexdigest()
        assert result.attachment_rows() == [
            {
                "filename": "poc.zip",
                "content_type": "application/zip",
                "size_bytes": len(payload),
                "sha256": meta.sha256,
            }
        ]

    def test_a_text_attachment_is_not_mistaken_for_the_body(self) -> None:
        from email.mime.text import MIMEText

        msg = MIMEMultipart()
        msg["Subject"] = "vulnerability with exploit"
        msg["From"] = "alice@example.com"
        msg.attach(MIMEText("<p>the html body</p>", "html"))
        poc = MIMEText("import os; os.system('id')")
        poc.add_header("Content-Disposition", "attachment", filename="poc.py")
        msg.attach(poc)

        result = parse_email_message(msg.as_bytes())

        assert "the html body" in result.body
        assert "os.system" not in result.body
        assert [a.filename for a in result.attachments] == ["poc.py"]

    def test_a_single_part_mail_has_no_attachments(self) -> None:
        msg = email.mime.text.MIMEText("plain")
        msg["From"] = "alice@example.com"
        assert parse_email_message(msg.as_bytes()).attachments == ()
        assert parse_pasted_report("plain").attachments == ()
