"""Dashboard surface of the intending-to-merge queue."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from django.test import Client

from franktheunicorn.dashboard.views import QUEUE_TABS
from tests.factories import ProjectFactory, PullRequestFactory


def _approved(project: object, number: int, days_ago_at: datetime, **extra: object) -> object:
    return PullRequestFactory(
        project=project,
        number=number,
        queue="intending-to-merge",
        merge_intent="approved",
        merge_intent_by="holdenk",
        merge_intent_at=days_ago_at,
        merge_intent_source="review",
        **extra,
    )


@pytest.mark.django_db
class TestQueueTab:
    def test_tab_is_listed_with_count(self, client: Client) -> None:
        assert any(t["key"] == "intending-to-merge" for t in QUEUE_TABS)
        project = ProjectFactory(owner="apache", repo="spark")
        _approved(project, 1, datetime(2026, 10, 1, tzinfo=UTC))
        PullRequestFactory(project=project, number=2, queue="review")

        response = client.get("/")

        assert response.status_code == 200
        html = response.content.decode()
        assert "Intending to Merge" in html
        assert '?queue=intending-to-merge"' in html

    def test_oldest_approval_first(self, client: Client) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        _approved(project, 10, datetime(2026, 10, 5, tzinfo=UTC), interest_score=0.9)
        _approved(project, 11, datetime(2026, 9, 1, tzinfo=UTC), interest_score=0.1)
        PullRequestFactory(
            project=project,
            number=12,
            queue="intending-to-merge",
            merge_intent="conditional",
            merge_intent_at=None,
            interest_score=0.95,
        )

        response = client.get("/?queue=intending-to-merge")

        numbers = [pr.number for pr in response.context["pull_requests"]]
        assert numbers == [11, 10, 12], "oldest approval first, undated last, score ignored"

    def test_badges_rendered(self, client: Client) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        _approved(
            project,
            20,
            datetime(2026, 10, 1, tzinfo=UTC),
            ci_status="pass",
            head_sha="new",
            merge_intent_commit_sha="old",
            merge_intent_excerpt="LGTM, thanks!",
        )
        PullRequestFactory(
            project=project,
            number=21,
            queue="intending-to-merge",
            merge_intent="conditional",
            merge_intent_by="holdenk",
            merge_intent_at=datetime(2026, 10, 2, tzinfo=UTC),
            ci_status="pending",
        )

        html = client.get("/?queue=intending-to-merge").content.decode()

        assert 'class="badge-intent approved"' in html
        assert 'class="badge-intent conditional"' in html
        assert "Approved pending CI" in html
        assert 'class="badge-ci pass"' in html
        assert 'class="badge-ci pending"' in html
        assert "Pushed since approval" in html
        assert "LGTM, thanks!" in html

    def test_other_queues_unaffected(self, client: Client) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        PullRequestFactory(project=project, number=30, queue="review", interest_score=0.1)
        PullRequestFactory(project=project, number=31, queue="review", interest_score=0.9)

        response = client.get("/?queue=review")

        assert [pr.number for pr in response.context["pull_requests"]] == [31, 30]
        assert "badge-intent" not in response.content.decode()


@pytest.mark.django_db
class TestDetailPage:
    def test_shows_who_when_and_excerpt(self, client: Client) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        pr = PullRequestFactory(
            project=project,
            number=40,
            queue="intending-to-merge",
            merge_intent="conditional",
            merge_intent_by="holdenk",
            merge_intent_at=datetime(2026, 10, 1, tzinfo=UTC),
            merge_intent_source="comment",
            merge_intent_excerpt="Looks good pending CI.",
            ci_status="fail",
            head_sha="b",
            merge_intent_commit_sha="a",
        )

        html = client.get(f"/pr/{pr.pk}/").content.decode()

        assert "Approved pending CI" in html
        assert "<strong>holdenk</strong>" in html
        assert "Looks good pending CI." in html
        assert 'class="badge-ci fail"' in html
        assert "Pushed since approval" in html
        assert "Back to intending-to-merge" in html

    def test_nothing_when_unset(self, client: Client) -> None:
        pr = PullRequestFactory(project=ProjectFactory(owner="apache", repo="spark"), number=41)
        html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert "badge-intent" not in html


@pytest.mark.django_db
class TestMergeQueuePage:
    def test_approved_pr_by_someone_else_is_listed(self, client: Client) -> None:
        """The merge queue used to be the operator's own PRs only."""
        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        _approved(
            project,
            50,
            datetime(2026, 10, 1, tzinfo=UTC),
            author="cloud-fan",
            is_operator_pr=False,
            ci_status="pass",
            approval_count=2,
            mergeable=True,
            merge_intent_excerpt="LGTM",
        )
        PullRequestFactory(project=project, number=51, author="someone", is_operator_pr=False)
        pc = ProjectConfig(
            owner="apache",
            repo="spark",
            merge_queue=MergeQueueConfig(enabled=True, required_approvals=2),
        )

        with patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]):
            response = client.get("/merge-queue/")

        html = response.content.decode()
        assert "apache/spark#50" in html
        assert "apache/spark#51" not in html
        assert "<th>Intent</th>" in html
        assert 'class="badge-intent approved"' in html
        assert "Ready" in html

    def test_own_pr_still_listed(self, client: Client) -> None:
        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        PullRequestFactory(project=project, number=60, is_operator_pr=True, queue="your-prs")
        pc = ProjectConfig(owner="apache", repo="spark", merge_queue=MergeQueueConfig(enabled=True))

        with patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]):
            html = client.get("/merge-queue/").content.decode()

        assert "apache/spark#60" in html
        assert "Your PR" in html
