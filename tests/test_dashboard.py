"""Tests for the dashboard views."""

from __future__ import annotations

import pytest
from django.test import Client

from franktheunicorn.core.models import (
    AgentFeedback,
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

    def test_stats_rejection_predictor_section(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=0.9,
            is_auto_suppressed=True,
        )
        ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=0.3,
            is_auto_suppressed=False,
        )
        response = client.get("/stats/")
        assert response.status_code == 200
        assert b"Rejection Predictor" in response.content
        assert b"Auto-Suppressed Findings" in response.content


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

    def test_workspace_cookie_persists_across_requests(
        self, client: Client, db_pr: PullRequest
    ) -> None:
        """Verify that set_workspace sets a cookie that subsequent requests use."""
        # Set workspace via POST
        response = client.post("/set-workspace/", {"workspace": "my-workspace"})
        assert response.status_code == 302
        assert "workspace" in response.cookies
        assert response.cookies["workspace"].value == "my-workspace"

        # Subsequent GET should have the cookie set (Django test client carries cookies)
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


@pytest.mark.django_db
class TestAgentFeedbackViews:
    """Tests for v1.25 agent feedback dashboard views."""

    def test_pr_detail_shows_agent_info(self, client: Client) -> None:
        pr = PullRequestFactory(
            ai_agent_source="claude-code",
            agent_session_url="https://claude.ai/code/session/abc123",
        )
        response = client.get(f"/pr/{pr.pk}/")
        assert response.status_code == 200
        assert b"claude-code" in response.content
        assert b"Send Feedback to Session" in response.content
        assert b"Open Session" in response.content

    def test_pr_detail_hides_agent_info_for_normal_pr(self, client: Client) -> None:
        pr = PullRequestFactory(ai_agent_source="")
        response = client.get(f"/pr/{pr.pk}/")
        assert response.status_code == 200
        assert b"Send Feedback to Session" not in response.content

    def test_compose_feedback(self, client: Client) -> None:
        pr = PullRequestFactory(
            ai_agent_source="claude-code",
            agent_session_url="https://claude.ai/code/session/abc123",
            title="AI fix",
        )
        ReviewDraftFactory(
            pull_request=pr,
            file_path="src/main.py",
            line_number=10,
            comment_body="Fix this bug.",
        )
        response = client.get(f"/pr/{pr.pk}/compose-feedback/")
        assert response.status_code == 200
        assert b"Compose Feedback" in response.content
        assert b"Fix this bug." in response.content
        assert b"assessment" in response.content

    def test_send_feedback_creates_record(self, client: Client) -> None:
        pr = PullRequestFactory(
            ai_agent_source="claude-code",
            agent_session_url="https://claude.ai/code/session/abc123",
        )
        response = client.post(
            f"/pr/{pr.pk}/send-feedback/",
            {
                "assessment": "good",
                "feedback_body": "Nice work on this PR!",
            },
        )
        assert response.status_code == 200
        assert b"Feedback recorded" in response.content
        fb = AgentFeedback.objects.get(pull_request=pr)
        assert fb.assessment == "good"
        assert fb.feedback_body == "Nice work on this PR!"
        assert fb.feedback_method == "session-url"

    def test_send_feedback_github_comment_method(self, client: Client) -> None:
        pr = PullRequestFactory(
            ai_agent_source="codex",
            agent_session_url="",
            agent_task_id="task_123",
        )
        response = client.post(
            f"/pr/{pr.pk}/send-feedback/",
            {
                "assessment": "needs-work",
                "feedback_body": "Please fix the tests.",
            },
        )
        assert response.status_code == 200
        fb = AgentFeedback.objects.get(pull_request=pr)
        assert fb.feedback_method == "github-comment"

    def test_send_feedback_empty_body_rejected(self, client: Client) -> None:
        pr = PullRequestFactory(ai_agent_source="claude-code")
        response = client.post(
            f"/pr/{pr.pk}/send-feedback/",
            {"assessment": "good", "feedback_body": "   "},
        )
        assert response.status_code == 200
        assert b"cannot be empty" in response.content
        assert AgentFeedback.objects.count() == 0

    def test_send_feedback_invalid_assessment_rejected(self, client: Client) -> None:
        pr = PullRequestFactory(ai_agent_source="claude-code")
        response = client.post(
            f"/pr/{pr.pk}/send-feedback/",
            {"assessment": "invalid-value", "feedback_body": "Some feedback"},
        )
        assert response.status_code == 200
        assert b"Invalid assessment" in response.content
        assert AgentFeedback.objects.count() == 0


@pytest.mark.django_db
class TestRejectionProbabilityDisplay:
    """Tests for v1.75 rejection probability display in dashboard."""

    def test_draft_with_rejection_probability(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=0.65,
            comment_body="Some nit.",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"P(reject): 0.65" in response.content

    def test_draft_without_rejection_probability(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=None,
            comment_body="Good finding.",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"P(reject)" not in response.content

    def test_likely_low_value_badge(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=0.65,
            is_auto_suppressed=False,
            comment_body="Style nit.",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"likely low-value" in response.content

    def test_suppressed_drafts_in_collapsible(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=0.9,
            is_auto_suppressed=True,
            comment_body="Auto-suppressed nit.",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"Suppressed Findings" in response.content
        assert b"Auto-suppressed nit." in response.content
        assert b"auto-suppressed" in response.content

    def test_suppressed_drafts_not_in_main_list(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=0.9,
            is_auto_suppressed=True,
            comment_body="Suppressed finding body.",
        )
        ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=0.2,
            is_auto_suppressed=False,
            comment_body="Visible finding body.",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        content = response.content.decode()
        # The suppressed finding should only appear in the suppressed section.
        main_section_end = content.index("Suppressed Findings")
        main_section = content[:main_section_end]
        assert "Visible finding body." in main_section
        assert "Suppressed finding body." not in main_section

    def test_suppressed_draft_has_action_buttons(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=0.9,
            is_auto_suppressed=True,
            comment_body="Suppressed but actionable.",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"Approve" in response.content

    def test_approve_suppressed_draft_unsuppresses(
        self, client: Client, db_pr: PullRequest
    ) -> None:
        draft = ReviewDraftFactory(
            pull_request=db_pr,
            rejection_probability=0.9,
            is_auto_suppressed=True,
            comment_body="Was suppressed.",
        )
        response = client.post(f"/draft/{draft.pk}/approve/")
        assert response.status_code == 200
        draft.refresh_from_db()
        assert draft.status == "accepted"
        assert draft.is_auto_suppressed is False

    def test_no_suppressed_section_when_none(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            is_auto_suppressed=False,
            comment_body="Normal finding.",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"Suppressed Findings" not in response.content
