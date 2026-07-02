"""Tests for forge-aware PR-diff fetching in ``process_pr``.

The review pipeline must fetch the diff from each project's *configured*
forge (via its ``ForgeClient``), not hard-coded public GitHub. When no client
is supplied it falls back to the public-GitHub dual-path ``DiffFetcher``, and
any fetch failure degrades gracefully to the changed-files placeholder.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import ProjectConfig
from tests.factories import PullRequestFactory


def _capture_diff() -> tuple[dict[str, Any], Any]:
    """A fake ``draft_review`` that records the ``diff`` kwarg it receives."""
    captured: dict[str, Any] = {}

    def fake_draft_review(pr: Any, pc: Any, **kwargs: Any) -> list[Any]:
        captured["diff"] = kwargs.get("diff")
        return []

    return captured, fake_draft_review


@pytest.mark.django_db
class TestProcessPrForgeAwareDiff:
    def test_prefers_forge_client_diff(self) -> None:
        from franktheunicorn.worker.runner import process_pr

        pr = PullRequestFactory(state="open")
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)

        forge_client = MagicMock()
        forge_client.get_pull_request_diff.return_value = "DIFF-FROM-FORGE"

        captured, fake_draft_review = _capture_diff()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake_draft_review):
            process_pr(pr, config, operator_config=None, force=True, forge_client=forge_client)

        # The diff came from the project's forge client, keyed on its own coords.
        forge_client.get_pull_request_diff.assert_called_once_with(
            pr.project.owner, pr.project.repo, pr.number
        )
        assert captured["diff"] == "DIFF-FROM-FORGE"

    def test_falls_back_to_diff_fetcher_without_client(self) -> None:
        from franktheunicorn.worker.runner import process_pr

        pr = PullRequestFactory(state="open")
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)

        mock_fetcher = MagicMock()
        mock_fetcher.fetch.return_value = MagicMock(raw_diff="DIFF-FROM-DIFFFETCHER")

        captured, fake_draft_review = _capture_diff()
        with (
            patch("franktheunicorn.review.drafter.draft_review", side_effect=fake_draft_review),
            patch(
                "franktheunicorn.data_access.github.diff_fetcher.DiffFetcher",
                return_value=mock_fetcher,
            ),
        ):
            # No forge_client → legacy public-GitHub dual-path fetcher.
            process_pr(pr, config, operator_config=None, force=True)

        assert captured["diff"] == "DIFF-FROM-DIFFFETCHER"

    def test_degrades_to_placeholder_when_forge_diff_fails(self) -> None:
        from franktheunicorn.worker.runner import process_pr

        pr = PullRequestFactory(state="open")
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)

        forge_client = MagicMock()
        forge_client.get_pull_request_diff.side_effect = RuntimeError("forge down")

        captured, fake_draft_review = _capture_diff()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake_draft_review):
            process_pr(pr, config, operator_config=None, force=True, forge_client=forge_client)

        # Fetch raised → empty diff; draft_review still runs on the placeholder.
        assert captured["diff"] == ""
