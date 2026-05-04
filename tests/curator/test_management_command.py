"""Tests for the curate_voice management command."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tests.curator.helpers import make_classified_comment as _make_classified
from tests.curator.helpers import make_raw_comment as _make_raw


class TestCurateVoiceCommand:
    """Tests for the curate_voice management command."""

    def test_command_exists(self) -> None:
        """The management command is discoverable by Django."""
        from django.core.management import get_commands

        commands = get_commands()
        assert "curate_voice" in commands

    def test_missing_project_argument(self) -> None:
        """Command should fail if --project is not provided."""
        with pytest.raises(CommandError):
            call_command("curate_voice")

    def test_invalid_project_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Command should reject a project name without a slash."""
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        with pytest.raises(CommandError, match="owner/repo"):
            call_command("curate_voice", project="noslash")

    def test_missing_github_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Command should fail if GITHUB_TOKEN is not set."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(CommandError, match="GITHUB_TOKEN"):
            call_command("curate_voice", project="org/repo")

    @patch("franktheunicorn.core.management.commands.curate_voice.Command.handle")
    def test_add_arguments_has_expected_args(self, mock_handle: MagicMock) -> None:
        """Verify the command accepts --project, --limit, --output-dir."""
        mock_handle.return_value = None
        out = StringIO()
        call_command(
            "curate_voice",
            project="org/repo",
            limit=50,
            output_dir="/tmp/test",
            stdout=out,
        )
        mock_handle.assert_called_once()
        _args, kwargs = mock_handle.call_args
        assert kwargs["project"] == "org/repo"
        assert kwargs["limit"] == 50
        assert kwargs["output_dir"] == "/tmp/test"

    @patch("franktheunicorn.curator.app.CuratorApp")
    @patch("franktheunicorn.curator.classifier.classify_comments")
    @patch("franktheunicorn.curator.scraper.scrape_review_comments")
    def test_full_flow(
        self,
        mock_scrape: MagicMock,
        mock_classify: MagicMock,
        mock_app_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test the full command flow with mocked dependencies."""
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

        raw_comments = [_make_raw("Fix bug"), _make_raw("Add test")]
        mock_scrape.return_value = raw_comments

        classified = [_make_classified("Fix bug"), _make_classified("Add test")]
        mock_classify.return_value = classified

        mock_app_instance = MagicMock()
        mock_app_cls.return_value = mock_app_instance

        out = StringIO()
        call_command("curate_voice", project="org/repo", limit=10, stdout=out)

        mock_scrape.assert_called_once_with("org", "repo", "fake-token", limit=10)
        mock_classify.assert_called_once_with(raw_comments)
        mock_app_cls.assert_called_once()
        mock_app_instance.run.assert_called_once()

    @patch("franktheunicorn.curator.scraper.scrape_review_comments")
    def test_no_comments_found(
        self,
        mock_scrape: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test the command exits gracefully when no comments are found."""
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        mock_scrape.return_value = []

        out = StringIO()
        call_command("curate_voice", project="org/repo", stdout=out)

        output = out.getvalue()
        assert "No comments found" in output

    @patch("franktheunicorn.curator.app.CuratorApp")
    @patch("franktheunicorn.curator.classifier.classify_comments")
    @patch("franktheunicorn.curator.scraper.scrape_review_comments")
    def test_output_dir_passed_through(
        self,
        mock_scrape: MagicMock,
        mock_classify: MagicMock,
        mock_app_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        """Test that --output-dir is passed to CuratorApp."""
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        mock_scrape.return_value = [_make_raw()]
        mock_classify.return_value = [_make_classified()]
        mock_app_instance = MagicMock()
        mock_app_cls.return_value = mock_app_instance

        out = StringIO()
        call_command(
            "curate_voice",
            project="org/repo",
            output_dir=str(tmp_path),
            stdout=out,
        )

        call_kwargs = mock_app_cls.call_args[1]
        assert call_kwargs["output_dir"] is not None

    @patch("franktheunicorn.curator.app.CuratorApp")
    @patch("franktheunicorn.curator.classifier.classify_comments")
    @patch("franktheunicorn.curator.scraper.scrape_review_comments")
    def test_default_output_dir_is_none(
        self,
        mock_scrape: MagicMock,
        mock_classify: MagicMock,
        mock_app_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that output_dir defaults to None when not specified."""
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        mock_scrape.return_value = [_make_raw()]
        mock_classify.return_value = [_make_classified()]
        mock_app_instance = MagicMock()
        mock_app_cls.return_value = mock_app_instance

        out = StringIO()
        call_command("curate_voice", project="org/repo", stdout=out)

        call_kwargs = mock_app_cls.call_args[1]
        assert call_kwargs["output_dir"] is None
