"""Tests for the dashboard views."""

from __future__ import annotations

import pytest
from django.test import Client

from franktheunicorn.core.models import (
    AntiPattern,
    DependencyChange,
    OperatorAction,
    PullRequest,
    ReviewDraft,
)
from tests.factories import (
    AntiPatternFactory,
    CostRecordFactory,
    PullRequestFactory,
    ReviewDraftFactory,
    TestRunFactory,
)


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


@pytest.mark.django_db
class TestQueueTabs:
    def test_index_queue_filter(self, client: Client, db_pr: PullRequest) -> None:
        db_pr.queue = "ai-generated"
        db_pr.save(update_fields=["queue"])
        response = client.get("/?queue=ai-generated")
        assert response.status_code == 200
        assert b"Fix flaky test" in response.content

    def test_index_queue_excludes_other_queues(self, client: Client, db_pr: PullRequest) -> None:
        db_pr.queue = "ai-generated"
        db_pr.save(update_fields=["queue"])
        response = client.get("/?queue=review")
        assert b"Fix flaky test" not in response.content

    def test_index_shows_tab_bar(self, client: Client) -> None:
        response = client.get("/")
        assert b"tab-bar" in response.content
        assert b"Review" in response.content
        assert b"Your PRs" in response.content
        assert b"AI-Generated" in response.content


@pytest.mark.django_db
class TestFindingActions:
    def test_approve_draft(self, client: Client, review_draft: ReviewDraft) -> None:
        response = client.post(f"/draft/{review_draft.pk}/approve/")
        assert response.status_code == 200
        review_draft.refresh_from_db()
        assert review_draft.status == "accepted"
        assert OperatorAction.objects.filter(action_type="accept_draft").exists()

    def test_reject_draft(self, client: Client, review_draft: ReviewDraft) -> None:
        response = client.post(
            f"/draft/{review_draft.pk}/reject/",
            {"reason": "too nitpicky"},
        )
        assert response.status_code == 200
        review_draft.refresh_from_db()
        assert review_draft.status == "rejected"
        assert review_draft.rejection_reason == "too nitpicky"
        assert OperatorAction.objects.filter(action_type="reject_draft").exists()
        # Anti-pattern auto-suggested from rejection reason.
        assert AntiPattern.objects.filter(pattern_text="too nitpicky").exists()

    def test_reject_draft_without_reason(self, client: Client, review_draft: ReviewDraft) -> None:
        response = client.post(f"/draft/{review_draft.pk}/reject/")
        assert response.status_code == 200
        review_draft.refresh_from_db()
        assert review_draft.status == "rejected"

    def test_edit_draft(self, client: Client, review_draft: ReviewDraft) -> None:
        response = client.post(
            f"/draft/{review_draft.pk}/edit/",
            {"edited_body": "Improved comment."},
        )
        assert response.status_code == 200
        review_draft.refresh_from_db()
        assert review_draft.status == "edited"
        assert review_draft.edited_body == "Improved comment."

    def test_edit_draft_unchanged(self, client: Client, review_draft: ReviewDraft) -> None:
        original = review_draft.comment_body
        response = client.post(
            f"/draft/{review_draft.pk}/edit/",
            {"edited_body": original},
        )
        assert response.status_code == 200
        review_draft.refresh_from_db()
        assert review_draft.status == "pending"  # unchanged

    def test_post_review_no_approved(self, client: Client, db_pr: PullRequest) -> None:
        response = client.post(f"/pr/{db_pr.pk}/post/")
        assert response.status_code == 200
        assert b"No approved findings" in response.content


@pytest.mark.django_db
class TestAntiPatternManager:
    def test_list_view(self, client: Client) -> None:
        response = client.get("/anti-patterns/")
        assert response.status_code == 200
        assert b"Anti-Pattern Manager" in response.content

    def test_create_pattern(self, client: Client, db_project: object) -> None:
        response = client.post(
            "/anti-patterns/create/",
            {"pattern_text": "formatting nit", "description": "skip formatting"},
        )
        assert response.status_code == 200
        assert AntiPattern.objects.filter(pattern_text="formatting nit").exists()

    def test_create_pattern_empty_rejected(self, client: Client) -> None:
        response = client.post("/anti-patterns/create/", {"pattern_text": ""})
        assert response.status_code == 400

    def test_delete_pattern(self, client: Client) -> None:
        ap = AntiPatternFactory()
        response = client.post(f"/anti-patterns/{ap.pk}/delete/")
        assert response.status_code == 200
        assert not AntiPattern.objects.filter(pk=ap.pk).exists()

    def test_toggle_pattern(self, client: Client) -> None:
        ap = AntiPatternFactory(is_active=True)
        response = client.post(f"/anti-patterns/{ap.pk}/toggle/")
        assert response.status_code == 200
        ap.refresh_from_db()
        assert ap.is_active is False

    def test_filter_by_project(self, client: Client, db_project: object) -> None:
        from franktheunicorn.core.models import Project

        project = Project.objects.first()
        AntiPatternFactory(project=project, pattern_text="proj-specific")
        response = client.get(f"/anti-patterns/?project={project.pk}")
        assert response.status_code == 200


@pytest.mark.django_db
class TestStatsView:
    def test_stats_empty(self, client: Client) -> None:
        response = client.get("/stats/")
        assert response.status_code == 200
        assert b"History" in response.content

    def test_stats_with_data(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(pull_request=db_pr, status="posted")
        CostRecordFactory(project=db_pr.project)
        response = client.get("/stats/")
        assert response.status_code == 200


@pytest.mark.django_db
class TestPRDetailWithTestRuns:
    def test_shows_test_run(self, client: Client, db_pr: PullRequest) -> None:
        TestRunFactory(
            pull_request=db_pr,
            status="completed",
            differential_verdict="good",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"GOOD" in response.content


@pytest.mark.django_db
class TestWorkspace:
    def test_set_workspace_redirects(self, client: Client) -> None:
        response = client.post("/set-workspace/", {"workspace": "work"})
        assert response.status_code == 302
        assert response.cookies.get("workspace")

    def test_index_with_workspace_cookie(self, client: Client, db_pr: PullRequest) -> None:
        client.cookies["workspace"] = "all"
        response = client.get("/")
        assert response.status_code == 200

    def test_post_review_no_token(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            status="accepted",
            comment_body="Post me.",
        )
        response = client.post(f"/pr/{db_pr.pk}/post/")
        assert response.status_code == 200
        # Token not configured so should say "Cannot post"
        assert b"Cannot post" in response.content or b"No approved" in response.content
