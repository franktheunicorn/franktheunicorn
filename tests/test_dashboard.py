"""Tests for the dashboard views."""

from __future__ import annotations

import pytest
from django.test import Client

from franktheunicorn.core.models import DependencyChange, PullRequest, ReviewDraft
from tests.factories import PullRequestFactory


@pytest.mark.django_db
class TestDashboardViews:
    def test_index_empty(self, client: Client) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert b"No pull requests in the" in response.content

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

    def test_pr_detail_with_drafts(
        self, client: Client, db_pr: PullRequest, review_draft: ReviewDraft
    ) -> None:
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"Consider adding a test" in response.content

    def test_pr_detail_404(self, client: Client) -> None:
        response = client.get("/pr/99999/")
        assert response.status_code == 404

    def test_index_orders_by_interest_score(self, client: Client, db_pr: PullRequest) -> None:
        PullRequestFactory(
            project=db_pr.project,
            number=db_pr.number + 1,
            github_id=db_pr.github_id + 1,
            title="Higher score PR",
            author="bob-dev",
            interest_score=db_pr.interest_score + 0.5,
        )
        response = client.get("/")
        assert response.status_code == 200
        assert response.content.index(b"Higher score PR") < response.content.index(
            db_pr.title.encode()
        )

    def test_index_excludes_closed_prs(self, client: Client, db_pr: PullRequest) -> None:
        PullRequestFactory(
            project=db_pr.project,
            number=db_pr.number + 1,
            github_id=db_pr.github_id + 1,
            title="Closed PR should not appear",
            author="bob-dev",
            state="closed",
        )
        response = client.get("/")
        assert response.status_code == 200
        assert b"Closed PR should not appear" not in response.content

    def test_pr_detail_with_dependency_changes(self, client: Client, db_pr: PullRequest) -> None:
        DependencyChange.objects.create(
            pull_request=db_pr,
            package_name="httpx",
            ecosystem="python",
            old_version="0.26.0",
            new_version="0.27.0",
            source_file="requirements.txt",
            changelog_url="https://github.com/encode/httpx/releases/tag/0.27.0",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"httpx" in response.content
        assert b"0.26.0" in response.content
        assert b"0.27.0" in response.content

    def test_pr_detail_score_breakdown_values(self, client: Client, db_pr: PullRequest) -> None:
        db_pr.score_breakdown = {"path_overlap": 15.0, "has_review_request": 20.0}
        db_pr.save(update_fields=["score_breakdown"])
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"path_overlap" in response.content
        assert b"15.0" in response.content
        assert b"has_review_request" in response.content
