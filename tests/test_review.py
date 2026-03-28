"""Tests for the review drafter and anti-pattern system."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import AntiPattern, Project, PullRequest, ReviewDraft
from franktheunicorn.review.antipattern import check_against_anti_patterns, record_anti_pattern
from franktheunicorn.review.drafter import draft_review


@pytest.mark.django_db
class TestDraftReview:
    def test_generates_drafts(
        self, db_pr: PullRequest, spark_project_config: ProjectConfig
    ) -> None:
        drafts = draft_review(db_pr, spark_project_config)
        assert len(drafts) > 0
        assert all(isinstance(d, ReviewDraft) for d in drafts)

    def test_drafts_are_deterministic(
        self, db_pr: PullRequest, spark_project_config: ProjectConfig
    ) -> None:
        """Same input should produce same output."""
        drafts1 = draft_review(db_pr, spark_project_config)
        # Clear drafts and regenerate
        ReviewDraft.objects.filter(pull_request=db_pr).delete()
        drafts2 = draft_review(db_pr, spark_project_config)
        assert len(drafts1) == len(drafts2)
        assert drafts1[0].comment_body == drafts2[0].comment_body
        assert drafts1[0].file_path == drafts2[0].file_path

    def test_draft_fields(self, db_pr: PullRequest, spark_project_config: ProjectConfig) -> None:
        drafts = draft_review(db_pr, spark_project_config)
        for d in drafts:
            assert d.file_path != ""
            assert d.line_number is not None and d.line_number > 0
            assert d.comment_body != ""
            assert d.status == "pending"
            assert 0.0 <= d.confidence <= 1.0


@pytest.mark.django_db
class TestAntiPattern:
    def test_record_new_pattern(self, db_project: Project) -> None:
        ap = record_anti_pattern(
            "nit: ",
            description="Avoid nitpicky comments",
            project=db_project,
        )
        assert ap.pattern_text == "nit: "
        assert ap.times_triggered == 0

    def test_record_existing_increments(self, db_project: Project) -> None:
        record_anti_pattern("nit: ", project=db_project)
        ap = record_anti_pattern("nit: ", project=db_project)
        assert ap.times_triggered == 1

    def test_check_matches(self, db_project: Project) -> None:
        AntiPattern.objects.create(
            pattern_text="nit:",
            project=db_project,
        )
        matches = check_against_anti_patterns("nit: fix the spacing here", db_project)
        assert len(matches) == 1

    def test_check_no_match(self, db_project: Project) -> None:
        AntiPattern.objects.create(
            pattern_text="nit:",
            project=db_project,
        )
        matches = check_against_anti_patterns("Great improvement!", db_project)
        assert len(matches) == 0
