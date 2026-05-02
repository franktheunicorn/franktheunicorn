"""
Integration test: full polling → scoring → storage → review drafting flow.

This tests the end-to-end path using mock data, verifying that all
components work together correctly.
"""

from __future__ import annotations

from typing import Any

import pytest

from franktheunicorn.backends.mock import MockGitHubClient
from franktheunicorn.backends.poller import poll_project
from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import PullRequest, ReviewDraft
from franktheunicorn.review.drafter import draft_review


@pytest.mark.django_db
@pytest.mark.integration
class TestFullPipeline:
    def test_poll_score_draft_flow(self, tmp_path: Any) -> None:
        """End-to-end: poll → score → store → draft reviews."""
        client = MockGitHubClient(tmp_path)
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            review_context="ASF governance",
            watched_paths=["sql/catalyst/", "python/pyspark/"],
            frequent_contributors=["cloud-fan"],
            tone="constructive",
        )

        # Step 1: Poll and ingest PRs
        prs = poll_project(client, config, operator_username="holdenk")
        assert len(prs) == 3

        # Step 2: Verify scores are assigned
        for pr in prs:
            assert pr.interest_score >= 0.0
            assert isinstance(pr.score_breakdown, dict)

        # Step 3: Generate review drafts for each PR
        total_drafts = 0
        for pr in prs:
            drafts = draft_review(pr, config)
            total_drafts += len(drafts)

        assert total_drafts > 0

        # Step 4: Verify drafts are persisted
        assert ReviewDraft.objects.count() == total_drafts
        assert all(d.status == "pending" for d in ReviewDraft.objects.all())

        # Step 5: Verify PR #42 (review requested for holdenk) has highest score
        pr42 = PullRequest.objects.get(number=42)
        pr44 = PullRequest.objects.get(number=44)  # dependabot PR
        assert pr42.interest_score > pr44.interest_score

    def test_idempotent_polling(self, tmp_path: Any) -> None:
        """Polling twice shouldn't duplicate PRs."""
        client = MockGitHubClient(tmp_path)
        config = ProjectConfig(owner="apache", repo="spark")

        poll_project(client, config, operator_username="holdenk")
        poll_project(client, config, operator_username="holdenk")

        assert PullRequest.objects.count() == 3
