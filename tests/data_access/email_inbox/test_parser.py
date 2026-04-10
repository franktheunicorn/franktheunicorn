"""Tests for email message parsing."""

from __future__ import annotations

import email.mime.text
from email.mime.multipart import MIMEMultipart

from franktheunicorn.data_access.email_inbox.parser import (
    _is_security_report,
    _strip_html,
    parse_email_message,
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

    def test_from_without_name(self) -> None:
        msg = email.mime.text.MIMEText("security vulnerability CVE report")
        msg["Subject"] = "Test"
        msg["From"] = "bare@example.com"
        msg["Message-ID"] = "<bare-123>"

        result = parse_email_message(msg.as_bytes())
        assert result.from_name == ""
        assert result.from_email == "bare@example.com"


class TestIsSecurityReport:
    def test_requires_two_keywords(self) -> None:
        assert _is_security_report("vulnerability", "exploit found") is True
        assert _is_security_report("hello", "world") is False

    def test_single_keyword_not_enough(self) -> None:
        assert _is_security_report("security", "please review") is False

    def test_multiple_keywords(self) -> None:
        assert (
            _is_security_report(
                "CVE-2024-1234 vulnerability",
                "proof of concept for RCE",
            )
            is True
        )


class TestStripHtml:
    def test_strips_tags(self) -> None:
        assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_handles_empty(self) -> None:
        assert _strip_html("") == ""
