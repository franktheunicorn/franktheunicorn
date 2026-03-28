"""Tests for Django models."""

from __future__ import annotations

import pytest

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
        project = Project.objects.create(owner="apache", repo="spark")
        assert str(project) == "apache/spark"
        assert project.full_name == "apache/spark"
        assert project.enabled is True

    def test_unique_constraint(self) -> None:
        Project.objects.create(owner="apache", repo="spark")
        with pytest.raises(Exception, match="UNIQUE constraint failed"):
            Project.objects.create(owner="apache", repo="spark")


@pytest.mark.django_db
class TestPullRequestModel:
    def test_create_pr(self, db_project: Project) -> None:
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=1001,
            number=42,
            title="Test PR",
            author="alice",
            url="https://example.com",
        )
        assert str(pr) == "#42 Test PR"
        assert pr.interest_score == 0.0
        assert pr.state == "open"

    def test_pr_json_fields(self, db_project: Project) -> None:
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=1002,
            number=43,
            title="PR with labels",
            author="bob",
            url="https://example.com",
            labels=["bug", "feature"],
            changed_files=["README.md"],
        )
        assert pr.labels == ["bug", "feature"]
        assert pr.changed_files == ["README.md"]


@pytest.mark.django_db
class TestReviewDraftModel:
    def test_create_draft(self, db_pr: PullRequest) -> None:
        draft = ReviewDraft.objects.create(
            pull_request=db_pr,
            file_path="src/main.py",
            line_number=10,
            comment_body="Consider adding a test.",
            confidence=0.7,
        )
        assert draft.status == "pending"
        assert "src/main.py" in str(draft)


@pytest.mark.django_db
class TestAntiPatternModel:
    def test_create_anti_pattern(self, db_project: Project) -> None:
        ap = AntiPattern.objects.create(
            pattern_text="nit: ",
            description="Avoid nitpicky comments",
            project=db_project,
        )
        assert "nit:" in str(ap)
        assert ap.times_triggered == 0

    def test_global_anti_pattern(self) -> None:
        ap = AntiPattern.objects.create(
            pattern_text="actually, ",
            description="Avoid 'well actually' comments",
            project=None,
        )
        assert ap.project is None


@pytest.mark.django_db
class TestOperatorActionModel:
    def test_create_action(self, db_pr: PullRequest) -> None:
        action = OperatorAction.objects.create(
            action_type="dismiss_pr",
            pull_request=db_pr,
            notes="Not relevant to me",
        )
        assert "dismiss_pr" in str(action)
