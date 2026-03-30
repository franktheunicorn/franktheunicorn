"""Tests for the dashboard views."""

from __future__ import annotations

import pytest
from django.test import Client

from franktheunicorn.core.models import PullRequest, ReviewDraft


@pytest.mark.django_db
class TestDashboardViews:
    def test_index_empty(self, client: Client) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert b"No pull requests ingested yet" in response.content

    def test_index_with_prs(self, client: Client, db_pr: PullRequest) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert b"Fix flaky test" in response.content
        assert b"alice-dev" in response.content

    def test_pr_detail(self, client: Client, db_pr: PullRequest) -> None:
        db_pr.score_breakdown = {"review_requested": 0.25}
        db_pr.save(update_fields=["score_breakdown"])
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"Fix flaky test" in response.content
        assert b"Score Breakdown" in response.content

    def test_pr_detail_with_drafts(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraft.objects.create(
            pull_request=db_pr,
            file_path="test.py",
            line_number=5,
            comment_body="Consider adding a test.",
            confidence=0.7,
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"Consider adding a test" in response.content

    def test_pr_detail_404(self, client: Client) -> None:
        response = client.get("/pr/99999/")
        assert response.status_code == 404
