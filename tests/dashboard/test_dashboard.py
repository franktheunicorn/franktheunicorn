"""Tests for the dashboard views."""

from __future__ import annotations

import pytest
from django.test import Client

from franktheunicorn.core.models import (
    AgentFeedback,
    AntiPattern,
    OperatorAction,
    PullRequest,
    ReviewDraft,
)
from tests.factories import (
    AntiPatternFactory,
    CostRecordFactory,
    DependencyChangeFactory,
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
        DependencyChangeFactory(
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

    def test_index_shows_findings_count(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(pull_request=db_pr, line_number=42, status="pending")
        ReviewDraftFactory(pull_request=db_pr, line_number=84, status="accepted")
        # Excluded: PR-level draft (no line_number).
        ReviewDraftFactory(pull_request=db_pr, line_number=None)
        # Excluded: rejected.
        ReviewDraftFactory(pull_request=db_pr, line_number=99, status="rejected")
        # Excluded: auto-suppressed.
        ReviewDraftFactory(pull_request=db_pr, line_number=120, is_auto_suppressed=True)

        response = client.get("/")
        assert response.status_code == 200
        assert b"2 findings" in response.content

    def test_index_omits_findings_count_when_zero(self, client: Client, db_pr: PullRequest) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert b'class="findings-badge"' not in response.content


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
        # The main draft-list section must contain the visible finding but not
        # render the suppressed finding as a standalone item.  We scope the
        # check to the draft-list div, which comes after the Agent Run Summary.
        draft_list_start = content.index('id="draft-list"')
        suppressed_section_start = content.index("Suppressed Findings")
        main_section = content[draft_list_start:suppressed_section_start]
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


@pytest.mark.django_db
class TestProjectFilters:
    """Tests for project-type and project filter GET params on the index view."""

    def _make_projects_and_prs(self) -> tuple:
        """Create two projects of different types, each with one open PR."""
        from tests.factories import ProjectFactory, PullRequestFactory

        asf_project = ProjectFactory(owner="apache", repo="spark", project_type="asf")
        personal_project = ProjectFactory(owner="holdenk", repo="my-app", project_type="personal")
        asf_pr = PullRequestFactory(
            project=asf_project, title="ASF PR", state="open", queue="review"
        )
        personal_pr = PullRequestFactory(
            project=personal_project, title="Personal PR", state="open", queue="review"
        )
        return asf_project, personal_project, asf_pr, personal_pr

    def test_filter_by_project_type_asf(self, client: Client) -> None:
        self._make_projects_and_prs()
        response = client.get("/?project_type=asf")
        assert response.status_code == 200
        assert b"ASF PR" in response.content
        assert b"Personal PR" not in response.content

    def test_filter_by_project_type_personal(self, client: Client) -> None:
        self._make_projects_and_prs()
        response = client.get("/?project_type=personal")
        assert response.status_code == 200
        assert b"Personal PR" in response.content
        assert b"ASF PR" not in response.content

    def test_filter_by_specific_project(self, client: Client) -> None:
        self._make_projects_and_prs()
        response = client.get("/?project=apache/spark")
        assert response.status_code == 200
        assert b"ASF PR" in response.content
        assert b"Personal PR" not in response.content

    def test_filter_by_project_and_type_combined(self, client: Client) -> None:
        self._make_projects_and_prs()
        response = client.get("/?project_type=asf&project=apache/spark")
        assert response.status_code == 200
        assert b"ASF PR" in response.content
        assert b"Personal PR" not in response.content

    def test_invalid_project_type_ignored_returns_all(self, client: Client) -> None:
        self._make_projects_and_prs()
        response = client.get("/?project_type=notatype")
        assert response.status_code == 200
        assert b"ASF PR" in response.content
        assert b"Personal PR" in response.content

    def test_invalid_project_slug_ignored_returns_all(self, client: Client) -> None:
        self._make_projects_and_prs()
        response = client.get("/?project=noslash")
        assert response.status_code == 200
        assert b"ASF PR" in response.content
        assert b"Personal PR" in response.content

    def test_available_project_types_only_has_active_types(self, client: Client) -> None:
        from tests.factories import ProjectFactory

        ProjectFactory(owner="apache", repo="kafka", project_type="asf", enabled=True)
        # Disabled project of type 'org' should not appear in the selector.
        ProjectFactory(owner="some-org", repo="tool", project_type="org", enabled=False)
        response = client.get("/")
        assert response.status_code == 200
        context = response.context
        available_types = [pt["key"] for pt in context["available_project_types"]]
        assert "asf" in available_types
        assert "org" not in available_types

    def test_available_projects_narrowed_when_type_set(self, client: Client) -> None:
        self._make_projects_and_prs()
        response = client.get("/?project_type=asf")
        assert response.status_code == 200
        context = response.context
        project_owners = [p["owner"] for p in context["available_projects"]]
        assert "apache" in project_owners
        assert "holdenk" not in project_owners

    def test_queue_counts_respect_project_type_filter(self, client: Client) -> None:
        _, personal_project, _, _ = self._make_projects_and_prs()
        # Add an extra PR for the personal project in the same queue.
        _extra_pr = PullRequestFactory(
            project=personal_project, title="Extra Personal PR", state="open", queue="review"
        )
        response = client.get("/?project_type=asf")
        assert response.status_code == 200
        context = response.context
        # Only the ASF PR should count towards the review queue badge.
        assert context["queue_counts"]["review"] == 1

    def test_queue_counts_respect_project_filter(self, client: Client) -> None:
        self._make_projects_and_prs()
        response = client.get("/?project=holdenk/my-app")
        assert response.status_code == 200
        context = response.context
        assert context["queue_counts"]["review"] == 1

    def test_filter_bar_rendered_in_html(self, client: Client) -> None:
        from tests.factories import ProjectFactory

        ProjectFactory(owner="apache", repo="spark", project_type="asf", enabled=True)
        response = client.get("/")
        assert response.status_code == 200
        assert b"filter-bar" in response.content
        assert b"project_type" in response.content
        assert b"filter-project" in response.content

    def test_tab_links_preserve_project_type_filter(self, client: Client) -> None:
        response = client.get("/?project_type=asf")
        assert response.status_code == 200
        assert b"project_type=asf" in response.content

    def test_tab_links_preserve_project_filter(self, client: Client) -> None:
        response = client.get("/?project=apache/spark")
        assert response.status_code == 200
        assert b"project=apache/spark" in response.content

    def test_no_suppressed_section_when_none(self, client: Client, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            is_auto_suppressed=False,
            comment_body="Normal finding.",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"Suppressed Findings" not in response.content


@pytest.mark.django_db
class TestAllCorePageTemplatesRender:
    """Smoke tests: every dashboard route renders without TemplateDoesNotExist."""

    def test_index_renders(self, client: Client) -> None:
        assert client.get("/").status_code == 200

    def test_pr_detail_renders(self, client: Client, db_pr: PullRequest) -> None:
        assert client.get(f"/pr/{db_pr.pk}/").status_code == 200

    def test_anti_patterns_renders(self, client: Client) -> None:
        assert client.get("/anti-patterns/").status_code == 200

    def test_stats_renders(self, client: Client) -> None:
        assert client.get("/stats/").status_code == 200

    def test_approve_draft_renders(self, client: Client, review_draft: ReviewDraft) -> None:
        assert client.post(f"/draft/{review_draft.pk}/approve/").status_code == 200

    def test_reject_draft_renders(self, client: Client, review_draft: ReviewDraft) -> None:
        assert client.post(f"/draft/{review_draft.pk}/reject/").status_code == 200

    def test_edit_draft_renders(self, client: Client, review_draft: ReviewDraft) -> None:
        resp = client.post(f"/draft/{review_draft.pk}/edit/", {"edited_body": "updated body"})
        assert resp.status_code == 200

    def test_post_review_renders(self, client: Client, db_pr: PullRequest) -> None:
        assert client.post(f"/pr/{db_pr.pk}/post/").status_code == 200

    def test_compose_feedback_renders(self, client: Client, db_pr: PullRequest) -> None:
        assert client.get(f"/pr/{db_pr.pk}/compose-feedback/").status_code == 200

    def test_send_feedback_renders(self, client: Client, db_pr: PullRequest) -> None:
        resp = client.post(
            f"/pr/{db_pr.pk}/send-feedback/",
            {"assessment": "good", "feedback_body": "Looks great"},
        )
        assert resp.status_code == 200

    def test_anti_pattern_create_renders(self, client: Client, db_project: object) -> None:
        resp = client.post("/anti-patterns/create/", {"pattern_text": "smoke test"})
        assert resp.status_code == 200

    def test_anti_pattern_toggle_renders(self, client: Client) -> None:
        ap = AntiPatternFactory()
        assert client.post(f"/anti-patterns/{ap.pk}/toggle/").status_code == 200

    def test_set_workspace_redirects(self, client: Client) -> None:
        assert client.post("/set-workspace/", {"workspace": "all"}).status_code == 302


@pytest.mark.django_db
class TestRecallDraft:
    def test_recall_not_posted(self, client: Client, review_draft: ReviewDraft) -> None:
        response = client.post(f"/draft/{review_draft.pk}/recall/")
        assert response.status_code == 200
        assert b"Cannot recall" in response.content

    def test_recall_no_comment_id(self, client: Client, db_pr: PullRequest) -> None:
        draft = ReviewDraftFactory(pull_request=db_pr, status="posted", github_comment_id=None)
        response = client.post(f"/draft/{draft.pk}/recall/")
        assert b"Cannot recall" in response.content


@pytest.mark.django_db
class TestDashboardV15:
    """Tests for v1.5 dashboard additions."""

    def test_pr_detail_with_jira_context(self, client: Client) -> None:
        pr = PullRequestFactory(
            jira_cache={"ticket_id": "SPARK-123", "summary": "Fix bug", "status": "Open"},
        )
        response = client.get(f"/pr/{pr.pk}/")
        assert response.status_code == 200
        assert response.context["jira_context"] is not None
        assert response.context["jira_context"]["ticket_id"] == "SPARK-123"

    def test_pr_detail_with_community_context(self, client: Client) -> None:
        pr = PullRequestFactory(
            community_context_cache={
                "sources": [{"type": "mailing-list", "name": "dev@"}],
                "query": "test",
            },
        )
        response = client.get(f"/pr/{pr.pk}/")
        assert response.status_code == 200
        assert response.context["community_context"] is not None

    def test_pr_detail_with_sentry_context(self, client: Client) -> None:
        pr = PullRequestFactory(
            sentry_context_cache={
                "issues": [{"title": "NPE in RDD.scala", "count": 42}],
            },
        )
        response = client.get(f"/pr/{pr.pk}/")
        assert response.status_code == 200
        assert response.context["sentry_context"] is not None

    def test_pr_detail_coderabbit_drafts_separated(self, client: Client) -> None:
        pr = PullRequestFactory()
        ReviewDraftFactory(pull_request=pr, sources=["agent"])
        ReviewDraftFactory(pull_request=pr, sources=["coderabbit"])
        ReviewDraftFactory(pull_request=pr, sources=["agent", "coderabbit"])
        response = client.get(f"/pr/{pr.pk}/")
        assert response.status_code == 200
        assert len(response.context["agent_drafts"]) == 1
        assert len(response.context["coderabbit_drafts"]) == 2

    def test_pr_detail_no_context_is_none(self, client: Client) -> None:
        pr = PullRequestFactory()
        response = client.get(f"/pr/{pr.pk}/")
        assert response.status_code == 200
        assert response.context["jira_context"] is None
        assert response.context["community_context"] is None
        assert response.context["sentry_context"] is None


@pytest.mark.django_db
class TestWorkspaceFiltering:
    """Tests for workspace project filtering in views."""

    def test_get_workspace_projects_returns_project_list(self, client: Client) -> None:
        from unittest.mock import MagicMock, patch

        mock_config = MagicMock()
        mock_config.workspaces = {
            "work": {"projects": ["apache/spark", "apache/flink"], "description": "Work"},
        }
        client.cookies["workspace"] = "work"
        with patch("franktheunicorn.config.loader.load_operator_config", return_value=mock_config):
            response = client.get("/")
        assert response.status_code == 200

    def test_get_workspace_list_includes_configured_workspaces(self, client: Client) -> None:
        from unittest.mock import MagicMock, patch

        mock_config = MagicMock()
        mock_config.workspaces = {
            "team-a": {"description": "Team A Projects", "projects": "*"},
        }
        with patch("franktheunicorn.config.loader.load_operator_config", return_value=mock_config):
            response = client.get("/")
        assert response.status_code == 200
        # Workspace list should include "Team A Projects"
        workspaces = response.context["workspaces"]
        labels = [w["label"] for w in workspaces]
        assert "Team A Projects" in labels

    def test_workspace_with_star_projects_does_not_filter(
        self, client: Client, db_pr: PullRequest
    ) -> None:
        from unittest.mock import MagicMock, patch

        mock_config = MagicMock()
        mock_config.workspaces = {"work": {"projects": "*", "description": "All"}}
        client.cookies["workspace"] = "work"
        with patch("franktheunicorn.config.loader.load_operator_config", return_value=mock_config):
            response = client.get("/")
        assert response.status_code == 200

    def test_workspace_config_exception_falls_back(
        self, client: Client, db_pr: PullRequest
    ) -> None:
        from unittest.mock import patch

        client.cookies["workspace"] = "nonexistent"
        with patch(
            "franktheunicorn.config.loader.load_operator_config",
            side_effect=Exception("config error"),
        ):
            response = client.get("/")
        assert response.status_code == 200

    def test_index_filters_by_workspace_projects(self, client: Client, db_pr: PullRequest) -> None:
        from unittest.mock import MagicMock, patch

        mock_config = MagicMock()
        mock_config.workspaces = {
            "work": {
                "projects": [f"{db_pr.project.owner}/{db_pr.project.repo}"],
                "description": "Work",
            },
        }
        client.cookies["workspace"] = "work"
        with patch("franktheunicorn.config.loader.load_operator_config", return_value=mock_config):
            response = client.get("/")
        assert response.status_code == 200


@pytest.mark.django_db
class TestRecallAndPostWithMock:
    """Tests for recall_draft and post_review with mocked GitHub client."""

    def test_recall_draft_success(self, client: Client, db_pr: PullRequest) -> None:
        from datetime import UTC, datetime
        from unittest.mock import MagicMock, patch

        draft = ReviewDraftFactory(
            pull_request=db_pr,
            status="posted",
            github_comment_id=123,
            posted_at=datetime.now(tz=UTC),
        )

        mock_poster = MagicMock()
        mock_poster.recall_comment.return_value = True

        with (
            patch("franktheunicorn.backends.github.GitHubClient"),
            patch("franktheunicorn.backends.poster.GitHubPoster", return_value=mock_poster),
            patch("django.conf.settings.FRANK_GITHUB_TOKEN", "test-token", create=True),
        ):
            response = client.post(f"/draft/{draft.pk}/recall/")

        assert response.status_code == 200

    def test_recall_draft_no_token(self, client: Client, db_pr: PullRequest) -> None:
        from datetime import UTC, datetime
        from unittest.mock import patch

        draft = ReviewDraftFactory(
            pull_request=db_pr,
            status="posted",
            github_comment_id=123,
            posted_at=datetime.now(tz=UTC),
        )

        with patch("django.conf.settings.FRANK_GITHUB_TOKEN", "", create=True):
            response = client.post(f"/draft/{draft.pk}/recall/")

        assert response.status_code == 200
        assert b"Cannot recall" in response.content

    def test_post_review_success(self, client: Client, db_pr: PullRequest) -> None:
        from unittest.mock import MagicMock, patch

        ReviewDraftFactory(
            pull_request=db_pr,
            status="accepted",
            comment_body="Good finding.",
        )

        mock_poster = MagicMock()
        mock_poster.post_review.return_value = {"id": 1}

        with (
            patch("franktheunicorn.backends.github.GitHubClient"),
            patch("franktheunicorn.backends.poster.GitHubPoster", return_value=mock_poster),
            patch("django.conf.settings.FRANK_GITHUB_TOKEN", "test-token", create=True),
        ):
            response = client.post(f"/pr/{db_pr.pk}/post/")

        assert response.status_code == 200
        assert b"Posted" in response.content

    def test_post_review_exception(self, client: Client, db_pr: PullRequest) -> None:
        from unittest.mock import patch

        ReviewDraftFactory(
            pull_request=db_pr,
            status="accepted",
            comment_body="Will fail.",
        )

        with (
            patch(
                "franktheunicorn.backends.github.GitHubClient",
                side_effect=Exception("connection failed"),
            ),
            patch("django.conf.settings.FRANK_GITHUB_TOKEN", "test-token", create=True),
        ):
            response = client.post(f"/pr/{db_pr.pk}/post/")

        assert response.status_code == 200
        assert b"Failed to post" in response.content


@pytest.mark.django_db
class TestAntiPatternCreateWithProject:
    def test_create_with_project_id(self, client: Client, db_project: object) -> None:
        from franktheunicorn.core.models import Project

        project = Project.objects.first()
        response = client.post(
            "/anti-patterns/create/",
            {"pattern_text": "test pattern", "project_id": str(project.pk)},
        )
        assert response.status_code == 200
        ap = AntiPattern.objects.get(pattern_text="test pattern")
        assert ap.project == project


@pytest.mark.django_db
class TestMergePRView:
    """Tests for the merge_pr POST view."""

    def test_merge_pr_not_enabled(self, client: Client) -> None:
        from unittest.mock import patch

        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig

        pr = PullRequestFactory()
        pc = ProjectConfig(
            owner=pr.project.owner,
            repo=pr.project.repo,
            merge_queue=MergeQueueConfig(enabled=False),
        )
        with patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]):
            response = client.post(f"/pr/{pr.pk}/merge/")
        assert response.status_code == 200
        assert b"not enabled" in response.content

    def test_merge_pr_not_eligible(self, client: Client) -> None:
        from unittest.mock import patch

        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig
        from franktheunicorn.worker.merge_queue import MergeEligibility

        pr = PullRequestFactory()
        pc = ProjectConfig(
            owner=pr.project.owner,
            repo=pr.project.repo,
            merge_queue=MergeQueueConfig(enabled=True),
        )
        ineligible = MergeEligibility(eligible=False, details=["CI failing"])
        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch(
                "franktheunicorn.worker.merge_queue.evaluate_merge_eligibility",
                return_value=ineligible,
            ),
        ):
            response = client.post(f"/pr/{pr.pk}/merge/")
        assert response.status_code == 200
        assert b"no longer eligible" in response.content

    def test_merge_pr_no_token(self, client: Client) -> None:
        from unittest.mock import patch

        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig
        from franktheunicorn.worker.merge_queue import MergeEligibility

        pr = PullRequestFactory()
        pc = ProjectConfig(
            owner=pr.project.owner,
            repo=pr.project.repo,
            merge_queue=MergeQueueConfig(enabled=True),
        )
        eligible = MergeEligibility(
            eligible=True, ci_pass=True, approvals_met=True, no_conflicts=True
        )
        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch(
                "franktheunicorn.worker.merge_queue.evaluate_merge_eligibility",
                return_value=eligible,
            ),
            patch("django.conf.settings.FRANK_GITHUB_TOKEN", "", create=True),
        ):
            response = client.post(f"/pr/{pr.pk}/merge/")
        assert response.status_code == 200
        assert b"GITHUB_TOKEN" in response.content

    def test_merge_pr_success(self, client: Client) -> None:
        from unittest.mock import patch

        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig
        from franktheunicorn.worker.merge_queue import MergeEligibility, MergeResult

        pr = PullRequestFactory()
        pc = ProjectConfig(
            owner=pr.project.owner,
            repo=pr.project.repo,
            merge_queue=MergeQueueConfig(enabled=True),
        )
        eligible = MergeEligibility(
            eligible=True, ci_pass=True, approvals_met=True, no_conflicts=True
        )
        success_result = MergeResult(success=True, method="squash")
        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch(
                "franktheunicorn.worker.merge_queue.evaluate_merge_eligibility",
                return_value=eligible,
            ),
            patch("franktheunicorn.worker.merge_queue.execute_merge", return_value=success_result),
            patch("franktheunicorn.backends.github.GitHubClient"),
            patch("django.conf.settings.FRANK_GITHUB_TOKEN", "test-token", create=True),
        ):
            response = client.post(f"/pr/{pr.pk}/merge/")
        assert response.status_code == 200
        assert b"Merged" in response.content

    def test_merge_pr_failure(self, client: Client) -> None:
        from unittest.mock import patch

        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig
        from franktheunicorn.worker.merge_queue import MergeEligibility, MergeResult

        pr = PullRequestFactory()
        pc = ProjectConfig(
            owner=pr.project.owner,
            repo=pr.project.repo,
            merge_queue=MergeQueueConfig(enabled=True),
        )
        eligible = MergeEligibility(
            eligible=True, ci_pass=True, approvals_met=True, no_conflicts=True
        )
        fail_result = MergeResult(success=False, error="conflict")
        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch(
                "franktheunicorn.worker.merge_queue.evaluate_merge_eligibility",
                return_value=eligible,
            ),
            patch("franktheunicorn.worker.merge_queue.execute_merge", return_value=fail_result),
            patch("franktheunicorn.backends.github.GitHubClient"),
            patch("django.conf.settings.FRANK_GITHUB_TOKEN", "test-token", create=True),
        ):
            response = client.post(f"/pr/{pr.pk}/merge/")
        assert response.status_code == 200
        assert b"Merge failed" in response.content


@pytest.mark.django_db
class TestMergeQueueView:
    """Tests for merge queue copy-command vs merge button behaviour."""

    def _make_eligible_pr(self) -> PullRequest:
        from tests.factories import ProjectFactory

        project = ProjectFactory(owner="apache", repo="spark")
        return PullRequestFactory(
            project=project,
            number=42,
            state="open",
            is_operator_pr=True,
            ci_status="pass",
            approval_count=2,
            interest_score=1.0,
        )

    def test_copy_button_shown_when_merge_script_configured(self, client: Client) -> None:
        from unittest.mock import patch

        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig
        from franktheunicorn.worker.merge_queue import MergeEligibility

        self._make_eligible_pr()
        pc = ProjectConfig(
            owner="apache",
            repo="spark",
            review_context="ASF",
            merge_queue=MergeQueueConfig(
                enabled=True,
                merge_script="dev/merge_spark_pr.py",
                required_approvals=2,
            ),
        )
        eligibility = MergeEligibility(
            eligible=True, ci_pass=True, approvals_met=True, no_conflicts=True
        )

        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch(
                "franktheunicorn.worker.merge_queue.evaluate_merge_eligibility",
                return_value=eligibility,
            ),
        ):
            response = client.get("/merge-queue/")

        assert response.status_code == 200
        assert b"Copy PR #" in response.content
        assert b'data-command="42"' in response.content
        assert b'<button type="submit">Merge</button>' not in response.content

    def test_merge_button_shown_when_no_script(self, client: Client) -> None:
        from unittest.mock import patch

        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig
        from franktheunicorn.worker.merge_queue import MergeEligibility

        self._make_eligible_pr()
        pc = ProjectConfig(
            owner="apache",
            repo="spark",
            review_context="ASF",
            merge_queue=MergeQueueConfig(enabled=True, required_approvals=2),
        )
        eligibility = MergeEligibility(
            eligible=True, ci_pass=True, approvals_met=True, no_conflicts=True
        )

        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch(
                "franktheunicorn.worker.merge_queue.evaluate_merge_eligibility",
                return_value=eligibility,
            ),
        ):
            response = client.get("/merge-queue/")

        assert response.status_code == 200
        assert b"Merge</button>" in response.content
        assert b"Copy PR #" not in response.content

    def test_blockers_shown_when_not_eligible(self, client: Client) -> None:
        from unittest.mock import patch

        from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig
        from franktheunicorn.worker.merge_queue import MergeEligibility

        self._make_eligible_pr()
        pc = ProjectConfig(
            owner="apache",
            repo="spark",
            review_context="ASF",
            merge_queue=MergeQueueConfig(enabled=True, required_approvals=2),
        )
        eligibility = MergeEligibility(
            eligible=False,
            ci_pass=False,
            approvals_met=True,
            no_conflicts=True,
            details=["CI status: fail (requires pass)"],
        )

        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch(
                "franktheunicorn.worker.merge_queue.evaluate_merge_eligibility",
                return_value=eligibility,
            ),
        ):
            response = client.get("/merge-queue/")

        assert response.status_code == 200
        assert b"CI status: fail" in response.content
        assert b"Copy PR #" not in response.content
        assert b"Merge</button>" not in response.content

    def test_empty_queue(self, client: Client) -> None:
        from unittest.mock import patch

        with patch("franktheunicorn.config.loader.load_project_configs", return_value=[]):
            response = client.get("/merge-queue/")

        assert response.status_code == 200
        assert b"No PRs in the merge queue" in response.content


@pytest.mark.django_db
class TestAgentRunSummary:
    """Tests for the agent run summary feature on the PR detail page."""

    def test_summary_in_context(self, client: Client, db_pr: PullRequest) -> None:
        """Agent run summary is always passed to the template context."""
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert "agent_run_summary" in response.context

    def test_summary_section_renders(self, client: Client, db_pr: PullRequest) -> None:
        """Agent Run Summary heading appears when there are configured agents."""
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        assert b"Agent Run Summary" in response.content

    def test_stub_agent_shows_as_ran_with_findings(
        self, client: Client, db_pr: PullRequest
    ) -> None:
        """When a draft with sources=['agent'] exists, stub agent shows as ran."""
        ReviewDraftFactory(
            pull_request=db_pr,
            sources=["agent"],
            comment_body="Consider a better approach.",
            file_path="src/lib.py",
            line_number=10,
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        summary = response.context["agent_run_summary"]
        stub_entry = next((e for e in summary if e["source"] == "agent"), None)
        assert stub_entry is not None
        assert stub_entry["did_run"] is True
        assert stub_entry["total"] == 1

    def test_coderabbit_extra_source_included(self, client: Client, db_pr: PullRequest) -> None:
        """CodeRabbit drafts not in configured list still appear as an extra source."""
        ReviewDraftFactory(
            pull_request=db_pr,
            sources=["coderabbit"],
            comment_body="CR finding.",
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        summary = response.context["agent_run_summary"]
        cr_entry = next((e for e in summary if e["source"] == "coderabbit"), None)
        assert cr_entry is not None
        assert cr_entry["did_run"] is True
        assert cr_entry["total"] == 1

    def test_multiple_sources_grouped_separately(self, client: Client, db_pr: PullRequest) -> None:
        """Drafts from different sources are grouped into separate summary entries."""
        ReviewDraftFactory(pull_request=db_pr, sources=["agent"])
        ReviewDraftFactory(pull_request=db_pr, sources=["agent"])
        ReviewDraftFactory(pull_request=db_pr, sources=["check:security"])
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        summary = response.context["agent_run_summary"]
        agent_entry = next((e for e in summary if e["source"] == "agent"), None)
        check_entry = next((e for e in summary if e["source"] == "check:security"), None)
        assert agent_entry is not None
        assert agent_entry["total"] == 2
        assert check_entry is not None
        assert check_entry["total"] == 1

    def test_status_counts_in_summary(self, client: Client, db_pr: PullRequest) -> None:
        """Per-status counts are accurate in the summary."""
        ReviewDraftFactory(pull_request=db_pr, sources=["agent"], status="accepted")
        ReviewDraftFactory(pull_request=db_pr, sources=["agent"], status="rejected")
        ReviewDraftFactory(pull_request=db_pr, sources=["agent"], status="pending")
        response = client.get(f"/pr/{db_pr.pk}/")
        summary = response.context["agent_run_summary"]
        entry = next(e for e in summary if e["source"] == "agent")
        assert entry["accepted"] == 1
        assert entry["rejected"] == 1
        assert entry["pending"] == 1

    def test_suppressed_counted_separately(self, client: Client, db_pr: PullRequest) -> None:
        """Auto-suppressed drafts are not counted in status totals."""
        ReviewDraftFactory(
            pull_request=db_pr, sources=["agent"], is_auto_suppressed=True, status="pending"
        )
        ReviewDraftFactory(
            pull_request=db_pr, sources=["agent"], is_auto_suppressed=False, status="pending"
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        summary = response.context["agent_run_summary"]
        entry = next(e for e in summary if e["source"] == "agent")
        assert entry["total"] == 2
        assert entry["suppressed"] == 1
        assert entry["active"] == 1
        assert entry["pending"] == 1  # only the non-suppressed one

    def test_line_level_summaries_in_findings(self, client: Client, db_pr: PullRequest) -> None:
        """Line-level summaries include file path, line number, and body snippet."""
        ReviewDraftFactory(
            pull_request=db_pr,
            sources=["agent"],
            file_path="src/core.py",
            line_number=42,
            comment_body="A" * 200,  # longer than 120 chars
        )
        response = client.get(f"/pr/{db_pr.pk}/")
        summary = response.context["agent_run_summary"]
        entry = next(e for e in summary if e["source"] == "agent")
        assert len(entry["findings"]) == 1
        finding = entry["findings"][0]
        assert finding["file_path"] == "src/core.py"
        assert finding["line_number"] == 42
        # body_snippet is truncated to 120 chars
        assert len(finding["body_snippet"]) == 120

    def test_not_run_agents_shown_in_response(self, client: Client, db_pr: PullRequest) -> None:
        """Agents configured (via mock) but with no findings show 'not run'."""
        from unittest.mock import MagicMock, patch

        mock_op_config = MagicMock()
        mock_op_config.llm_backends = []
        mock_op_config.coderabbit.enabled = True  # CR configured but no CR drafts
        mock_op_config.personality = "frank"

        mock_pc = MagicMock()
        mock_pc.llm_checks = []

        with (
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=mock_op_config,
            ),
            patch(
                "franktheunicorn.config.loader.get_project_config",
                return_value=mock_pc,
            ),
        ):
            response = client.get(f"/pr/{db_pr.pk}/")

        assert response.status_code == 200
        summary = response.context["agent_run_summary"]
        cr_entry = next((e for e in summary if e["source"] == "coderabbit"), None)
        assert cr_entry is not None
        assert cr_entry["did_run"] is False

    def test_html_shows_ran_checkmark(self, client: Client, db_pr: PullRequest) -> None:
        """HTML output contains run indicator for an agent that ran."""
        ReviewDraftFactory(pull_request=db_pr, sources=["agent"])
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        # The ✓ is represented as &#10003; in the template
        assert b"ran" in response.content

    def test_html_shows_not_run(self, client: Client, db_pr: PullRequest) -> None:
        """HTML output says 'not run' for configured-but-not-executed agents."""
        from unittest.mock import MagicMock, patch

        mock_op_config = MagicMock()
        mock_op_config.llm_backends = []
        mock_op_config.coderabbit.enabled = True
        mock_op_config.personality = "frank"
        mock_pc = MagicMock()
        mock_pc.llm_checks = []

        with (
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=mock_op_config,
            ),
            patch(
                "franktheunicorn.config.loader.get_project_config",
                return_value=mock_pc,
            ),
        ):
            response = client.get(f"/pr/{db_pr.pk}/")

        assert b"not run" in response.content

    def test_build_agent_run_summary_directly(self, db_pr: PullRequest) -> None:
        """Unit-test build_agent_run_summary helper directly."""
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.dashboard.views import build_agent_run_summary

        ReviewDraftFactory(pull_request=db_pr, sources=["claude"], status="accepted")
        ReviewDraftFactory(pull_request=db_pr, sources=["claude"], status="rejected")

        op_cfg = OperatorConfig(llm_backends=[LLMBackendConfig(provider="claude")])
        result = build_agent_run_summary(db_pr, op_cfg, None)

        claude_entry = next(e for e in result if e["source"] == "claude")
        assert claude_entry["did_run"] is True
        assert claude_entry["total"] == 2
        assert claude_entry["accepted"] == 1
        assert claude_entry["rejected"] == 1
        assert len(claude_entry["findings"]) == 2

    def test_draft_source_key_helper(self) -> None:
        """_draft_source_key returns correct primary key."""
        from unittest.mock import MagicMock

        from franktheunicorn.dashboard.views import _draft_source_key

        draft_with_sources = MagicMock()
        draft_with_sources.sources = ["claude"]
        draft_with_sources.backend_used = "claude"
        assert _draft_source_key(draft_with_sources) == "claude"

        draft_no_sources = MagicMock()
        draft_no_sources.sources = []
        draft_no_sources.backend_used = "openai"
        assert _draft_source_key(draft_no_sources) == "openai"

        draft_nothing = MagicMock()
        draft_nothing.sources = []
        draft_nothing.backend_used = ""
        assert _draft_source_key(draft_nothing) == "unknown"
