"""Tests for the read-only IMAP inbox fetcher."""

from __future__ import annotations

import email.mime.text
from email.message import Message
from unittest.mock import MagicMock, patch

from franktheunicorn.config.models import SecurityEmailConfig
from franktheunicorn.data_access.email_inbox.fetcher import (
    fetch_security_emails,
    tag_ingested_messages,
)


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


def _tag_config(email_config: SecurityEmailConfig, tag: str) -> SecurityEmailConfig:
    return email_config.model_copy(update={"ingested_tag": tag})


def _wire_tag_conn(
    mock_imap: MagicMock,
    capabilities: bytes = b"IMAP4rev1 UIDPLUS",
    search_hits: bytes = b"7",
) -> None:
    """Wire the mock for the tagging path: select, capability, search, store."""
    mock_imap.select.return_value = ("OK", [b"3"])
    mock_imap.capability.return_value = ("OK", [capabilities])
    mock_imap.search.return_value = ("OK", [search_hits])
    mock_imap.store.return_value = ("OK", [b""])


class TestTagIngestedMessages:
    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_no_tag_configured_never_connects(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        result = tag_ingested_messages(email_config, ["<sec-1>"])
        assert result.tagged == 0
        mock_connect.assert_not_called()

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_no_message_ids_never_connects(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        result = tag_ingested_messages(_tag_config(email_config, "frank/ingested"), [])
        assert result.tagged == 0
        mock_connect.assert_not_called()

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_gmail_server_gets_a_gmail_label(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_tag_conn(mock_imap, capabilities=b"IMAP4rev1 X-GM-EXT-1 UIDPLUS")

        result = tag_ingested_messages(_tag_config(email_config, "frank/ingested"), ["<sec-1>"])

        assert result.tagged == 1
        mock_imap.search.assert_called_once_with(None, "HEADER", "Message-ID", '"<sec-1>"')
        mock_imap.store.assert_called_once_with("7", "+X-GM-LABELS.SILENT", '("frank/ingested")')

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_plain_imap_server_gets_a_keyword(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_tag_conn(mock_imap)  # no X-GM-EXT-1

        # Spaces are legal in the config (Gmail labels allow them) but not in
        # an IMAP keyword atom, so the fallback sanitizes them.
        result = tag_ingested_messages(_tag_config(email_config, "in frank"), ["<sec-1>"])

        assert result.tagged == 1
        mock_imap.store.assert_called_once_with("7", "+FLAGS.SILENT", "(in_frank)")

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_tags_only_never_seen_and_selects_read_write(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_tag_conn(mock_imap, capabilities=b"X-GM-EXT-1")

        tag_ingested_messages(_tag_config(email_config, "frank/ingested"), ["<sec-1>"])

        # STORE needs a read-write select; the fetch path stays readonly=True.
        assert mock_imap.select.call_args.kwargs.get("readonly") is not True
        # The only write is the one tag — \Seen is never part of it.
        for call in mock_imap.store.call_args_list:
            assert "Seen" not in call.args[1] and "Seen" not in call.args[2]

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_message_not_found_is_counted_missing(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_tag_conn(mock_imap, search_hits=b"")

        result = tag_ingested_messages(_tag_config(email_config, "frank/ingested"), ["<gone-1>"])

        assert result.missing == 1
        assert result.tagged == 0
        mock_imap.store.assert_not_called()

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_unsearchable_message_id_is_skipped(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_tag_conn(mock_imap)

        result = tag_ingested_messages(
            _tag_config(email_config, "frank/ingested"), ['<bad"id>', "<ok-1>"]
        )

        assert result.skipped == 1
        assert result.tagged == 1
        mock_imap.search.assert_called_once_with(None, "HEADER", "Message-ID", '"<ok-1>"')

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_connection_error_is_reported_not_raised(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        mock_connect.side_effect = ConnectionError("refused")
        result = tag_ingested_messages(_tag_config(email_config, "frank/ingested"), ["<sec-1>"])
        assert result.error == "connection failed"
        assert result.tagged == 0

    @patch("franktheunicorn.data_access.email_inbox.fetcher._connect")
    def test_rejected_store_is_counted_failed(
        self, mock_connect: MagicMock, email_config: SecurityEmailConfig
    ) -> None:
        mock_imap = MagicMock()
        mock_connect.return_value = mock_imap
        _wire_tag_conn(mock_imap)
        mock_imap.store.return_value = ("NO", [b"keywords not permitted"])

        result = tag_ingested_messages(_tag_config(email_config, "frank/ingested"), ["<sec-1>"])

        assert result.failed == 1
        assert result.tagged == 0
