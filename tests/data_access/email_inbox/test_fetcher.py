"""Tests for the read-only IMAP inbox fetcher."""

from __future__ import annotations

import email.mime.text
from email.message import Message
from unittest.mock import MagicMock, patch

from franktheunicorn.config.models import SecurityEmailConfig
from franktheunicorn.data_access.email_inbox.fetcher import fetch_security_emails


def _msg(subject: str, body: str, from_addr: str, mid: str) -> Message:
    m = email.mime.text.MIMEText(body)
    m["Subject"] = subject
    m["From"] = from_addr
    m["Message-ID"] = mid
    m["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
    return m


def _wire_single_message(mock_imap: MagicMock, msg: Message, mid: str) -> None:
    """Wire the mock so one message flows through header-peek + full-peek."""
    mock_imap.select.return_value = ("OK", [b"1"])
    mock_imap.search.return_value = ("OK", [b"1"])

    header_blob = f"Message-ID: {mid}\r\n\r\n".encode()

    def fetch_side_effect(msg_id: bytes, spec: str) -> tuple[str, list]:
        if "HEADER.FIELDS" in spec:
            return ("OK", [(b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {40}", header_blob)])
        return ("OK", [(b"1 (BODY[] {1234}", msg.as_bytes())])

    mock_imap.fetch.side_effect = fetch_side_effect


class TestFetchSecurityEmails:
    def test_returns_empty_when_disabled(self) -> None:
        result = fetch_security_emails(SecurityEmailConfig(enabled=False))
        assert result.examined == []

    def test_returns_error_when_missing_host(self) -> None:
        config = SecurityEmailConfig(enabled=True, imap_host="", imap_user="test")
        result = fetch_security_emails(config)
        assert result.examined == []
        assert result.error == "incomplete config"

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_examines_and_classifies_messages(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        msg = _msg(
            "Security vulnerability report",
            "There is a vulnerability in the auth module. Exploit: run the PoC script.",
            "reporter@test.com",
            "<fetch-test-1>",
        )
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_single_message(mock_imap, msg, "<fetch-test-1>")

        result = fetch_security_emails(email_config)

        assert len(result.examined) == 1
        assert result.examined[0].subject == "Security vulnerability report"
        assert result.examined[0].is_security_report is True

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_is_read_only_never_marks_seen(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        msg = _msg("vulnerability exploit", "rce here", "r@test.com", "<ro-1>")
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_single_message(mock_imap, msg, "<ro-1>")

        fetch_security_emails(email_config)

        # The mailbox is opened read-only and we never set \Seen.
        mock_imap.select.assert_called_once()
        assert mock_imap.select.call_args.kwargs.get("readonly") is True
        mock_imap.store.assert_not_called()
        # Every fetch uses PEEK (does not implicitly set \Seen).
        for call in mock_imap.fetch.call_args_list:
            assert "PEEK" in call.args[1]

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_non_security_email_is_examined_but_not_flagged(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        msg = _msg("PR review request", "Please review my latest PR.", "dev@test.com", "<non-1>")
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_single_message(mock_imap, msg, "<non-1>")

        result = fetch_security_emails(email_config)
        assert len(result.examined) == 1
        assert result.examined[0].is_security_report is False

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_dedups_already_seen_message_ids(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        msg = _msg("vulnerability exploit rce", "body", "r@test.com", "<dup-1>")
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_single_message(mock_imap, msg, "<dup-1>")

        result = fetch_security_emails(email_config, already_seen_message_ids={"<dup-1>"})
        assert result.examined == []
        assert result.already_scanned == 1

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_handles_connection_error(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        mock_connect.side_effect = ConnectionError("Connection refused")
        result = fetch_security_emails(email_config)
        assert result.examined == []
        assert result.error == "connection failed"

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_handles_empty_inbox(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        mock_imap.select.return_value = ("OK", [b"0"])
        mock_imap.search.return_value = ("OK", [b""])

        result = fetch_security_emails(email_config)
        assert result.examined == []
        assert result.unread_total == 0
