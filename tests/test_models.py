"""Tests for Django models."""

from __future__ import annotations

import pytest
from django.db import IntegrityError
from django.utils import timezone
from tests.factories import (
    AntiPatternFactory,
    OperatorActionFactory,
    ProjectFactory,
    PullRequestFactory,
    ReviewDraftFactory,
)

from franktheunicorn.core.models import (
    AntiPattern,
    OperatorAction,
    Project,
    PullRequest,
    ReviewDraft,
)


@pytest.mark.django_db
class TestProjectModel:
    def test_create_project(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        assert str(project) == "apache/spark"
        assert project.full_name == "apache/spark"
        assert project.enabled is True

    def test_unique_constraint(self) -> None:
        ProjectFactory(owner="apache", repo="spark")
        with pytest.raises(IntegrityError):
            ProjectFactory(owner="apache", repo="spark")

    def test_defaults(self) -> None:
        project = ProjectFactory()
        assert project.enabled is True
        assert project.review_context == "general open-source"
        assert project.created_at is not None
        assert project.updated_at is not None

    def test_ordering(self) -> None:
        p2 = ProjectFactory(owner="zeta", repo="alpha")
        p1 = ProjectFactory(owner="alpha", repo="beta")
        results = list(Project.objects.all())
        assert results == [p1, p2]


@pytest.mark.django_db
class TestPullRequestModel:
    def test_create_pr(self, db_project: Project) -> None:
        pr = PullRequestFactory(
            project=db_project,
            github_id=1001,
            number=42,
            title="Test PR",
            author="alice",
        )
        assert str(pr) == "#42 Test PR"
        assert pr.interest_score == 0.0
        assert pr.state == "open"

    def test_pr_json_fields(self, db_project: Project) -> None:
        pr = PullRequestFactory(
            project=db_project,
            number=43,
            labels=["bug", "feature"],
            changed_files=["README.md"],
        )
        assert pr.labels == ["bug", "feature"]
        assert pr.changed_files == ["README.md"]

    def test_unique_constraint(self, db_project: Project) -> None:
        PullRequestFactory(project=db_project, number=99)
        with pytest.raises(IntegrityError):
            PullRequestFactory(project=db_project, number=99)

    def test_defaults(self) -> None:
        pr = PullRequestFactory()
        assert pr.state == "open"
        assert pr.interest_score == 0.0
        assert pr.is_draft is False
        assert pr.likely_ai_generated is False
        assert pr.labels == []
        assert pr.requested_reviewers == []
        assert pr.changed_files == []
        assert pr.score_breakdown == {}
        assert pr.additions == 0
        assert pr.deletions == 0

    def test_json_default_independence(self) -> None:
        """Default list/dict fields should be independent instances."""
        pr1 = PullRequestFactory()
        pr2 = PullRequestFactory()
        pr1.labels.append("bug")
        pr1.save()
        pr2.refresh_from_db()
        assert pr2.labels == []

    def test_ordering(self, db_project: Project) -> None:
        now = timezone.now()
        pr_low = PullRequestFactory(
            project=db_project, number=1, interest_score=1.0, github_updated_at=now
        )
        pr_high = PullRequestFactory(
            project=db_project, number=2, interest_score=5.0, github_updated_at=now
        )
        results = list(PullRequest.objects.filter(project=db_project))
        assert results == [pr_high, pr_low]

    def test_json_roundtrip_nested(self, db_project: Project) -> None:
        breakdown = {"path_overlap": 0.8, "reviewer_requested": True, "details": {"sub": [1, 2]}}
        pr = PullRequestFactory(
            project=db_project, number=50, score_breakdown=breakdown
        )
        pr.refresh_from_db()
        assert pr.score_breakdown == breakdown

    def test_relationship_count(self, db_project: Project) -> None:
        PullRequestFactory(project=db_project, number=1)
        PullRequestFactory(project=db_project, number=2)
        PullRequestFactory(project=db_project, number=3)
        assert db_project.pull_requests.count() == 3


@pytest.mark.django_db
class TestReviewDraftModel:
    def test_create_draft(self, db_pr: PullRequest) -> None:
        draft = ReviewDraftFactory(
            pull_request=db_pr,
            file_path="src/main.py",
            line_number=10,
            comment_body="Consider adding a test.",
            confidence=0.7,
        )
        assert draft.status == "pending"
        assert "src/main.py" in str(draft)

    def test_defaults(self) -> None:
        draft = ReviewDraftFactory()
        assert draft.status == "pending"
        assert draft.confidence == 0.5
        assert draft.edited_body == ""
        assert draft.suggestion == ""

    def test_ordering(self, db_pr: PullRequest) -> None:
        d1 = ReviewDraftFactory(pull_request=db_pr)
        d2 = ReviewDraftFactory(pull_request=db_pr)
        results = list(ReviewDraft.objects.filter(pull_request=db_pr))
        # Ordered by -created_at, so most recent first
        assert results[0] == d2
        assert results[1] == d1

    def test_filter_by_status(self, db_pr: PullRequest) -> None:
        ReviewDraftFactory(pull_request=db_pr, status="pending")
        ReviewDraftFactory(pull_request=db_pr, status="accepted")
        ReviewDraftFactory(pull_request=db_pr, status="rejected")
        assert db_pr.review_drafts.filter(status="pending").count() == 1
        assert db_pr.review_drafts.filter(status="accepted").count() == 1


@pytest.mark.django_db
class TestAntiPatternModel:
    def test_create_anti_pattern(self, db_project: Project) -> None:
        ap = AntiPatternFactory(
            pattern_text="nit: ",
            description="Avoid nitpicky comments",
            project=db_project,
        )
        assert "nit:" in str(ap)
        assert ap.times_triggered == 0

    def test_global_anti_pattern(self) -> None:
        ap = AntiPatternFactory(project=None)
        assert ap.project is None

    def test_defaults(self) -> None:
        ap = AntiPatternFactory()
        assert ap.weight == 1.0
        assert ap.times_triggered == 0
        assert ap.project is None

    def test_global_vs_project_query(self) -> None:
        project = ProjectFactory()
        AntiPatternFactory(project=project)
        AntiPatternFactory(project=project)
        AntiPatternFactory(project=None)
        assert project.anti_patterns.count() == 2
        assert AntiPattern.objects.filter(project__isnull=True).count() == 1

    def test_ordering(self) -> None:
        ap_low = AntiPatternFactory(weight=0.5, times_triggered=1)
        ap_high = AntiPatternFactory(weight=2.0, times_triggered=0)
        results = list(AntiPattern.objects.all())
        assert results[0] == ap_high
        assert results[1] == ap_low


@pytest.mark.django_db
class TestOperatorActionModel:
    def test_create_action(self, db_pr: PullRequest) -> None:
        action = OperatorActionFactory(
            action_type="dismiss_pr",
            pull_request=db_pr,
            notes="Not relevant to me",
        )
        assert "dismiss_pr" in str(action)

    def test_defaults(self) -> None:
        action = OperatorActionFactory()
        assert action.action_type == "accept_draft"
        assert action.review_draft is None
        assert action.notes == ""

    def test_ordering(self) -> None:
        a1 = OperatorActionFactory()
        a2 = OperatorActionFactory()
        results = list(OperatorAction.objects.all())
        assert results[0] == a2
        assert results[1] == a1


@pytest.mark.django_db
class TestCascadeDeletes:
    def test_project_delete_cascades_to_prs(self) -> None:
        project = ProjectFactory()
        PullRequestFactory(project=project, number=1)
        PullRequestFactory(project=project, number=2)
        assert PullRequest.objects.count() == 2
        project.delete()
        assert PullRequest.objects.count() == 0

    def test_project_delete_cascades_to_anti_patterns(self) -> None:
        project = ProjectFactory()
        AntiPatternFactory(project=project)
        assert AntiPattern.objects.count() == 1
        project.delete()
        assert AntiPattern.objects.count() == 0

    def test_pr_delete_cascades_to_drafts(self) -> None:
        pr = PullRequestFactory()
        ReviewDraftFactory(pull_request=pr)
        ReviewDraftFactory(pull_request=pr)
        assert ReviewDraft.objects.count() == 2
        pr.delete()
        assert ReviewDraft.objects.count() == 0

    def test_pr_delete_sets_null_on_action(self) -> None:
        pr = PullRequestFactory()
        action = OperatorActionFactory(pull_request=pr)
        pr.delete()
        action.refresh_from_db()
        assert action.pull_request is None

    def test_draft_delete_sets_null_on_action(self) -> None:
        draft = ReviewDraftFactory()
        action = OperatorActionFactory(
            action_type="accept_draft",
            review_draft=draft,
            pull_request=draft.pull_request,
        )
        draft.delete()
        action.refresh_from_db()
        assert action.review_draft is None

    def test_project_delete_cascades_through_pr_to_drafts(self) -> None:
        project = ProjectFactory()
        pr = PullRequestFactory(project=project, number=1)
        ReviewDraftFactory(pull_request=pr)
        ReviewDraftFactory(pull_request=pr)
        project.delete()
        assert PullRequest.objects.count() == 0
        assert ReviewDraft.objects.count() == 0
