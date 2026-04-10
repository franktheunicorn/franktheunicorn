"""Tests for IMAP inbox fetcher."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from franktheunicorn.config.models import SecurityEmailConfig
from franktheunicorn.data_access.email_inbox.fetcher import fetch_security_emails


class TestFetchSecurityEmails:
    def test_returns_empty_when_disabled(self) -> None:
        config = SecurityEmailConfig(enabled=False)
        assert fetch_security_emails(config) == []

    def test_returns_empty_when_missing_host(self) -> None:
        config = SecurityEmailConfig(enabled=True, imap_host="", imap_user="test")
        assert fetch_security_emails(config) == []

    def test_returns_empty_when_missing_user(self) -> None:
        config = SecurityEmailConfig(enabled=True, imap_host="imap.test.com", imap_user="")
        assert fetch_security_emails(config) == []

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_fetches_and_parses_messages(
        self,
        mock_connect: MagicMock,
        email_config: SecurityEmailConfig,
    ) -> None:
        import email.mime.text

        # Create a mock security email.
        msg = email.mime.text.MIMEText(
            "There is a vulnerability in the auth module.\n"
            "Exploit: run the proof of concept script."
        )
        msg["Subject"] = "Security vulnerability report"
        msg["From"] = "reporter@test.com"
        msg["Message-ID"] = "<fetch-test-1>"
        msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"

        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.search.return_value = ("OK", [b"1"])
        mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {1234})", msg.as_bytes())])

        results = fetch_security_emails(email_config)

        assert len(results) == 1
        assert results[0].subject == "Security vulnerability report"
        assert results[0].is_security_report is True
        mock_imap.store.assert_called_once()  # marked as read
        mock_imap.logout.assert_called_once()

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_skips_non_security_emails(
        self,
        mock_connect: MagicMock,
        email_config: SecurityEmailConfig,
    ) -> None:
        import email.mime.text

        msg = email.mime.text.MIMEText("Please review my latest PR.")
        msg["Subject"] = "PR review request"
        msg["From"] = "dev@test.com"
        msg["Message-ID"] = "<non-sec-1>"

        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.search.return_value = ("OK", [b"1"])
        mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822 {500})", msg.as_bytes())])

        results = fetch_security_emails(email_config)
        assert len(results) == 0

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_handles_connection_error(
        self,
        mock_connect: MagicMock,
        email_config: SecurityEmailConfig,
    ) -> None:
        mock_connect.side_effect = ConnectionError("Connection refused")
        results = fetch_security_emails(email_config)
        assert results == []

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_handles_empty_inbox(
        self,
        mock_connect: MagicMock,
        email_config: SecurityEmailConfig,
    ) -> None:
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        mock_imap.select.return_value = ("OK", [b"0"])
        mock_imap.search.return_value = ("OK", [b""])

        results = fetch_security_emails(email_config)
        assert results == []
