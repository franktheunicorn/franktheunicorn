"""Tests for the mailing list IMAP fetcher."""

from __future__ import annotations

import email.mime.multipart
import email.mime.text
from unittest.mock import MagicMock, patch

from franktheunicorn.config.models import CommunitySourceConfig
from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.mailing_list.imap_fetcher import fetch_mailing_list_imap

_CONNECT = "franktheunicorn.data_access.mailing_list.imap_fetcher._connect"
_IMAP_SSL = "franktheunicorn.data_access.mailing_list.imap_fetcher.imaplib.IMAP4_SSL"
_IMAP_PLAIN = "franktheunicorn.data_access.mailing_list.imap_fetcher.imaplib.IMAP4"


def _config(
    *,
    imap_host: str = "imap.example.com",
    imap_user: str = "bot@example.com",
    use_ssl: bool = True,
) -> CommunitySourceConfig:
    return CommunitySourceConfig(
        type="mailing-list",
        name="private@spark.apache.org",
        imap_host=imap_host,
        imap_port=993,
        imap_user=imap_user,
        imap_pass="secret",
        imap_folder="INBOX",
        use_ssl=use_ssl,
    )


def _message(
    subject: str,
    body: str = "Discussion about the change.",
    from_hdr: str = "Alice Dev <alice@example.com>",
) -> bytes:
    msg = email.mime.text.MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_hdr
    msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
    msg["Message-ID"] = "<msg-1@example.com>"
    return msg.as_bytes()


def _fetch_response(raw: bytes) -> tuple[str, list[object]]:
    return ("OK", [(b"1 (RFC822 {%d}" % len(raw), raw)])


class TestFetchMailingListImap:
    def test_returns_empty_when_missing_host(self) -> None:
        with patch(_CONNECT) as mock_connect:
            result = fetch_mailing_list_imap(_config(imap_host=""), "rebase")
        assert result.threads == []
        assert result.query == "rebase"
        mock_connect.assert_not_called()

    def test_returns_empty_when_missing_user(self) -> None:
        with patch(_CONNECT) as mock_connect:
            result = fetch_mailing_list_imap(_config(imap_user=""), "rebase")
        assert result.threads == []
        mock_connect.assert_not_called()

    @patch(_CONNECT)
    def test_fetches_and_parses_matching_subject(self, mock_connect: MagicMock) -> None:
        raw = _message("Re: SPARK-123 fix the rebase logic")
        conn = MagicMock()
        mock_connect.return_value = conn
        conn.search.return_value = ("OK", [b"1"])
        conn.fetch.return_value = _fetch_response(raw)

        result = fetch_mailing_list_imap(_config(), "rebase", blame_hit=True)

        assert len(result.threads) == 1
        thread = result.threads[0]
        assert thread.subject == "Re: SPARK-123 fix the rebase logic"
        assert thread.participants == ["Alice Dev"]
        assert "Discussion about the change." in thread.snippet
        assert thread.url == "<msg-1@example.com>"
        assert thread.list_name == "private@spark.apache.org"
        assert "SPARK-123" in thread.pr_references
        assert thread.blame_hit is True
        assert thread.fetched_via == FetchMethod.API
        conn.select.assert_called_once_with("INBOX")
        conn.logout.assert_called_once()

    @patch(_CONNECT)
    def test_filters_non_matching_subject(self, mock_connect: MagicMock) -> None:
        raw = _message("Unrelated weekly status email")
        conn = MagicMock()
        mock_connect.return_value = conn
        conn.search.return_value = ("OK", [b"1"])
        conn.fetch.return_value = _fetch_response(raw)

        result = fetch_mailing_list_imap(_config(), "rebase")
        assert result.threads == []
        conn.logout.assert_called_once()

    @patch(_CONNECT)
    def test_empty_search_result(self, mock_connect: MagicMock) -> None:
        conn = MagicMock()
        mock_connect.return_value = conn
        conn.search.return_value = ("OK", [b""])

        result = fetch_mailing_list_imap(_config(), "rebase")
        assert result.threads == []
        conn.fetch.assert_not_called()
        conn.logout.assert_called_once()

    @patch(_CONNECT)
    def test_search_status_not_ok(self, mock_connect: MagicMock) -> None:
        conn = MagicMock()
        mock_connect.return_value = conn
        conn.search.return_value = ("NO", [b""])

        result = fetch_mailing_list_imap(_config(), "rebase")
        assert result.threads == []

    @patch(_CONNECT)
    def test_skips_messages_with_bad_fetch(self, mock_connect: MagicMock) -> None:
        conn = MagicMock()
        mock_connect.return_value = conn
        conn.search.return_value = ("OK", [b"1 2"])
        # First id: fetch fails; second id: not a (header, body) tuple.
        conn.fetch.side_effect = [("NO", [None]), ("OK", [b"flags-only"])]

        result = fetch_mailing_list_imap(_config(), "rebase")
        assert result.threads == []
        assert conn.fetch.call_count == 2

    @patch(_CONNECT)
    def test_handles_connection_error(self, mock_connect: MagicMock) -> None:
        mock_connect.side_effect = ConnectionError("refused")
        result = fetch_mailing_list_imap(_config(), "rebase")
        assert result.threads == []

    @patch(_CONNECT)
    def test_handles_search_exception(self, mock_connect: MagicMock) -> None:
        conn = MagicMock()
        mock_connect.return_value = conn
        conn.search.side_effect = OSError("dropped")

        result = fetch_mailing_list_imap(_config(), "rebase")
        assert result.threads == []
        conn.logout.assert_called_once()

    @patch(_CONNECT)
    def test_multipart_body_and_address_only_sender(self, mock_connect: MagicMock) -> None:
        outer = email.mime.multipart.MIMEMultipart()
        outer["Subject"] = "rebase needed"
        outer["From"] = "bob@example.com"  # no display name
        outer["Date"] = "Tue, 02 Jan 2024 09:00:00 +0000"
        outer["Message-ID"] = "<mp-1@example.com>"
        outer.attach(email.mime.text.MIMEText("plain part with rebase details"))
        raw = outer.as_bytes()

        conn = MagicMock()
        mock_connect.return_value = conn
        conn.search.return_value = ("OK", [b"1"])
        conn.fetch.return_value = _fetch_response(raw)

        result = fetch_mailing_list_imap(_config(), "rebase")
        assert len(result.threads) == 1
        assert result.threads[0].participants == ["bob@example.com"]
        assert "plain part with rebase details" in result.threads[0].snippet


class TestConnect:
    @patch(_IMAP_SSL)
    def test_uses_ssl_connection(self, mock_ssl: MagicMock) -> None:
        conn = mock_ssl.return_value
        conn.search.return_value = ("OK", [b""])

        fetch_mailing_list_imap(_config(use_ssl=True), "rebase")

        mock_ssl.assert_called_once_with("imap.example.com", 993)
        conn.login.assert_called_once_with("bot@example.com", "secret")

    @patch(_IMAP_PLAIN)
    def test_uses_plain_connection(self, mock_plain: MagicMock) -> None:
        conn = mock_plain.return_value
        conn.search.return_value = ("OK", [b""])

        fetch_mailing_list_imap(_config(use_ssl=False), "rebase")

        mock_plain.assert_called_once_with("imap.example.com", 993)
        conn.login.assert_called_once_with("bot@example.com", "secret")
