"""Tests for WIP PR skipping and graduation (circle-back)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.backends.mock import MockGitHubClient
from franktheunicorn.backends.poller import _route_pr_to_queue, poll_project
from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import PullRequest
from franktheunicorn.scoring.moderation import is_wip_title
from tests.factories import PullRequestFactory


class TestIsWipTitle:
    def test_wip_bracket_prefix(self) -> None:
        assert is_wip_title("[WIP] do a thing")

    def test_wip_colon_prefix(self) -> None:
        assert is_wip_title("WIP: not ready yet")

    def test_draft_colon_prefix(self) -> None:
        assert is_wip_title("Draft: exploring API changes")

    def test_draft_bracket_prefix(self) -> None:
        assert is_wip_title("[draft] rough sketch")

    def test_case_insensitive(self) -> None:
        assert is_wip_title("[wip] lowercase")
        assert is_wip_title("WIP: UPPERCASE")

    def test_non_wip_titles(self) -> None:
        assert not is_wip_title("Fix auth regression")
        assert not is_wip_title("Add new feature")
        assert not is_wip_title("This is not a WIP PR")
        assert not is_wip_title("")


@pytest.mark.django_db
class TestRouteToWipQueue:
    def _make_config(self, skip_wip: bool = True) -> ProjectConfig:
        return ProjectConfig(owner="apache", repo="spark", skip_wip=skip_wip)

    def test_draft_pr_routed_to_wip(self) -> None:
        pr = PullRequestFactory(is_draft=True, title="Add feature")
        config = self._make_config(skip_wip=True)
        _route_pr_to_queue(pr, "holdenk", project_config=config)
        assert pr.queue == "wip"

    def test_wip_title_routed_to_wip(self) -> None:
        pr = PullRequestFactory(is_draft=False, title="WIP: not ready")
        config = self._make_config(skip_wip=True)
        _route_pr_to_queue(pr, "holdenk", project_config=config)
        assert pr.queue == "wip"

    def test_draft_bracket_title_routed_to_wip(self) -> None:
        pr = PullRequestFactory(is_draft=False, title="[WIP] experimenting")
        config = self._make_config(skip_wip=True)
        _route_pr_to_queue(pr, "holdenk", project_config=config)
        assert pr.queue == "wip"

    def test_non_wip_not_routed_to_wip(self) -> None:
        pr = PullRequestFactory(is_draft=False, title="Fix the thing")
        config = self._make_config(skip_wip=True)
        _route_pr_to_queue(pr, "holdenk", project_config=config)
        assert pr.queue != "wip"

    def test_skip_wip_false_draft_not_routed_to_wip(self) -> None:
        pr = PullRequestFactory(is_draft=True, title="WIP: draft")
        config = self._make_config(skip_wip=False)
        _route_pr_to_queue(pr, "holdenk", project_config=config)
        assert pr.queue != "wip"

    def test_no_config_draft_not_routed_to_wip(self) -> None:
        pr = PullRequestFactory(is_draft=True, title="WIP: draft")
        _route_pr_to_queue(pr, "holdenk", project_config=None)
        assert pr.queue != "wip"

    def test_operator_pr_takes_priority_over_wip(self) -> None:
        pr = PullRequestFactory(is_draft=True, title="WIP: my own work", author="holdenk")
        config = self._make_config(skip_wip=True)
        _route_pr_to_queue(pr, "holdenk", project_config=config)
        # Operator's own draft goes to wip (so they're not bothered reviewing their own WIP)
        assert pr.queue == "wip"


class _WipMockClient(MockGitHubClient):
    """Returns one draft PR and one ready PR."""

    def __init__(self, tmp_path: Path, *, draft: bool, title: str = "Add feature") -> None:
        super().__init__(tmp_path)
        self._draft = draft
        self._title = title

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        return [
            {
                "number": 101,
                "id": 1010,
                "title": self._title,
                "user": {"login": "contributor"},
                "state": "open",
                "html_url": f"https://github.com/{owner}/{repo}/pull/101",
                "diff_url": "",
                "body": "Some changes",
                "labels": [],
                "requested_reviewers": [],
                "assignees": [],
                "draft": self._draft,
                "additions": 10,
                "deletions": 2,
                "created_at": "2026-05-01T10:00:00Z",
                "updated_at": "2026-05-01T10:00:00Z",
            }
        ]

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return {}

    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return []


@pytest.mark.django_db
class TestWipPollerIntegration:
    def test_draft_pr_poll_lands_in_wip_queue(self, tmp_path: Path) -> None:
        client = _WipMockClient(tmp_path, draft=True)
        config = ProjectConfig(owner="test", repo="wip-test", skip_wip=True)
        prs = poll_project(client, config, operator_username="holdenk")
        assert len(prs) == 1
        assert prs[0].queue == "wip"

    def test_wip_title_pr_lands_in_wip_queue(self, tmp_path: Path) -> None:
        client = _WipMockClient(tmp_path, draft=False, title="WIP: not done")
        config = ProjectConfig(owner="test", repo="wip-test", skip_wip=True)
        prs = poll_project(client, config, operator_username="holdenk")
        assert prs[0].queue == "wip"

    def test_ready_pr_not_in_wip_queue(self, tmp_path: Path) -> None:
        client = _WipMockClient(tmp_path, draft=False, title="Add feature")
        config = ProjectConfig(owner="test", repo="wip-test", skip_wip=True)
        prs = poll_project(client, config, operator_username="holdenk")
        assert prs[0].queue != "wip"

    def test_graduation_rerouts_on_next_poll(self, tmp_path: Path) -> None:
        """A PR that was WIP should be re-routed once it graduates."""
        # First poll: draft PR → wip queue
        client_draft = _WipMockClient(tmp_path, draft=True, title="WIP: rough draft")
        config = ProjectConfig(owner="test", repo="wip-test", skip_wip=True)
        prs = poll_project(client_draft, config, operator_username="holdenk")
        assert prs[0].queue == "wip"
        pr_pk = prs[0].pk

        # Second poll: same PR number, now no longer WIP
        client_ready = _WipMockClient(tmp_path, draft=False, title="Add amazing feature")
        prs2 = poll_project(client_ready, config, operator_username="holdenk")

        assert len(prs2) == 1
        pr_after = PullRequest.objects.get(pk=pr_pk)
        assert pr_after.queue != "wip", "PR should have graduated out of the wip queue"

    def test_skip_wip_false_keeps_default_routing(self, tmp_path: Path) -> None:
        """When skip_wip=False, draft PRs are reviewed normally (not parked in wip)."""
        client = _WipMockClient(tmp_path, draft=True)
        config = ProjectConfig(owner="test", repo="wip-test", skip_wip=False)
        prs = poll_project(client, config, operator_username="holdenk")
        assert prs[0].queue != "wip"


@pytest.mark.django_db
class TestProcessPrSkipsWip:
    def test_process_pr_skips_wip_queue(self) -> None:
        from franktheunicorn.worker.runner import process_pr

        pr = PullRequestFactory(queue="wip", state="open")
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)

        # draft_review is locally imported inside process_pr; patching at source.
        mock_draft_review = MagicMock(return_value=[])
        with patch("franktheunicorn.review.drafter.draft_review", mock_draft_review):
            result = process_pr(pr, config, operator_config=None)

        assert result == []
        mock_draft_review.assert_not_called()

    def test_process_pr_force_ignores_wip_queue(self) -> None:
        from franktheunicorn.worker.runner import process_pr

        pr = PullRequestFactory(queue="wip", state="open")
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)

        mock_draft_review = MagicMock(return_value=[])
        with patch("franktheunicorn.review.drafter.draft_review", mock_draft_review):
            process_pr(pr, config, operator_config=None, force=True)

        mock_draft_review.assert_called_once()
