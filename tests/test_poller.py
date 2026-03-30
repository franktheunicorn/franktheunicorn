"""Tests for the poller service."""

from __future__ import annotations

from typing import Any

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Project, PullRequest
from franktheunicorn.github.mock import MockGitHubClient
from franktheunicorn.github.poller import poll_project


@pytest.mark.django_db
class TestPoller:
    def test_poll_creates_project_and_prs(self, tmp_path: Any) -> None:
        client = MockGitHubClient(tmp_path)
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            watched_paths=["sql/catalyst/"],
            frequent_contributors=["cloud-fan"],
        )
        prs = poll_project(client, config, operator_username="holdenk")
        assert len(prs) == 3
        assert Project.objects.filter(owner="apache", repo="spark").exists()
        assert PullRequest.objects.count() == 3

    def test_poll_updates_existing_prs(self, tmp_path: Any) -> None:
        """Polling twice should update, not duplicate."""
        client = MockGitHubClient(tmp_path)
        config = ProjectConfig(owner="apache", repo="spark")
        poll_project(client, config, operator_username="holdenk")
        poll_project(client, config, operator_username="holdenk")
        assert PullRequest.objects.count() == 3

    def test_poll_scores_prs(self, tmp_path: Any) -> None:
        client = MockGitHubClient(tmp_path)
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            watched_paths=["sql/"],
        )
        prs = poll_project(client, config, operator_username="holdenk")
        # PR #42 has holdenk as requested reviewer, so should score > 0
        pr42 = next(p for p in prs if p.number == 42)
        assert pr42.interest_score > 0
        assert len(pr42.score_breakdown) > 0
