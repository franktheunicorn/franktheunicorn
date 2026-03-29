"""Tests for the interest scoring service."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Project, PullRequest
from franktheunicorn.scoring.scorer import (
    _is_likely_bot,
    _path_overlap_score,
    score_pull_request,
)


@pytest.mark.django_db
class TestScoring:
    def test_operator_is_author(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=2001,
            number=100,
            title="Operator's PR",
            author="holdenk",
            url="https://example.com",
            changed_files=["README.md"],
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "operator_is_author" in breakdown
        assert _score > 0
        # Operator should NOT get new_contributor bump
        assert "new_contributor" not in breakdown

    def test_review_requested(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=2002,
            number=101,
            title="Review requested",
            author="someone",
            url="https://example.com",
            requested_reviewers=["holdenk"],
            changed_files=["README.md"],
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "review_requested" in breakdown

    def test_path_overlap(self, db_project: Project, spark_project_config: ProjectConfig) -> None:
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=2003,
            number=102,
            title="Catalyst change",
            author="someone",
            url="https://example.com",
            changed_files=["sql/catalyst/rules/Opt.scala", "sql/catalyst/trees/Tree.scala"],
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "path_overlap" in breakdown

    def test_frequent_contributor(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=2004,
            number=103,
            title="From known contributor",
            author="cloud-fan",
            url="https://example.com",
            changed_files=["README.md"],
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "frequent_contributor" in breakdown
        # Known contributor should NOT get new_contributor bump
        assert "new_contributor" not in breakdown

    def test_new_contributor(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=2005,
            number=104,
            title="First contribution",
            author="brand-new-person",
            url="https://example.com",
            changed_files=["README.md"],
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "new_contributor" in breakdown

    def test_returning_contributor_not_new(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        """Author with prior PRs in the project should NOT get new_contributor bump."""
        # Create an older PR from the same author
        PullRequest.objects.create(
            project=db_project,
            github_id=2010,
            number=110,
            title="Previous contribution",
            author="returning-person",
            url="https://example.com",
            changed_files=["README.md"],
        )
        # New PR from the same author
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=2011,
            number=111,
            title="Second contribution",
            author="returning-person",
            url="https://example.com",
            changed_files=["README.md"],
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "new_contributor" not in breakdown

    def test_bot_penalty(self, db_project: Project, spark_project_config: ProjectConfig) -> None:
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=2006,
            number=105,
            title="Bump deps",
            author="dependabot[bot]",
            url="https://example.com",
            changed_files=["requirements.txt"],
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "ai_generated_penalty" in breakdown

    def test_large_pr_penalty(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=2007,
            number=106,
            title="Massive refactor",
            author="someone",
            url="https://example.com",
            changed_files=["README.md"],
            additions=400,
            deletions=200,
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "large_pr_penalty" in breakdown

    def test_score_clamped(self, db_project: Project, spark_project_config: ProjectConfig) -> None:
        """Score should be between 0.0 and 1.0."""
        pr = PullRequest.objects.create(
            project=db_project,
            github_id=2008,
            number=107,
            title="Normal PR",
            author="someone",
            url="https://example.com",
            changed_files=["README.md"],
        )
        score, _ = score_pull_request(pr, spark_project_config, "holdenk")
        assert 0.0 <= score <= 1.0


class TestHelpers:
    def test_is_likely_bot(self) -> None:
        assert _is_likely_bot("dependabot[bot]") is True
        assert _is_likely_bot("renovate") is True
        assert _is_likely_bot("alice-dev") is False

    def test_path_overlap_score(self) -> None:
        assert (
            _path_overlap_score(
                ["sql/catalyst/a.scala", "core/b.scala"],
                ["sql/catalyst/"],
            )
            == 0.5
        )

    def test_path_overlap_score_empty(self) -> None:
        assert _path_overlap_score([], ["sql/"]) == 0.0

    def test_path_overlap_score_full_match(self) -> None:
        assert (
            _path_overlap_score(
                ["src/a.py", "src/b.py"],
                ["src/"],
            )
            == 1.0
        )
