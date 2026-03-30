"""Tests for the interest scoring service."""

from __future__ import annotations

from typing import Any

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.scoring.scorer import (
    _is_likely_bot,
    _path_overlap_score,
    score_pull_request,
)


@pytest.mark.django_db
class TestScoring:
    def test_operator_is_author(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(number=100, author="holdenk", title="Operator's PR")
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "operator_is_author" in breakdown
        assert _score > 0
        # Operator should NOT get new_contributor bump
        assert "new_contributor" not in breakdown

    def test_review_requested(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(number=101, title="Review requested", requested_reviewers=["holdenk"])
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "review_requested" in breakdown

    def test_path_overlap(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(
            number=102,
            title="Catalyst change",
            changed_files=["sql/catalyst/rules/Opt.scala", "sql/catalyst/trees/Tree.scala"],
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "path_overlap" in breakdown

    def test_frequent_contributor(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(number=103, author="cloud-fan", title="From known contributor")
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "frequent_contributor" in breakdown
        # Known contributor should NOT get new_contributor bump
        assert "new_contributor" not in breakdown

    def test_new_contributor(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(number=104, author="brand-new-person", title="First contribution")
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "new_contributor" in breakdown

    def test_returning_contributor_not_new(
        self, make_pr: Any, spark_project_config: ProjectConfig
    ) -> None:
        """Author with prior PRs in the project should NOT get new_contributor bump."""
        make_pr(number=110, author="returning-person", title="Previous contribution")
        pr = make_pr(number=111, author="returning-person", title="Second contribution")
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "new_contributor" not in breakdown

    def test_bot_penalty(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(
            number=105,
            author="dependabot[bot]",
            title="Bump deps",
            changed_files=["requirements.txt"],
        )
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "ai_generated_penalty" in breakdown

    def test_large_pr_penalty(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(number=106, title="Massive refactor", additions=400, deletions=200)
        _score, breakdown = score_pull_request(pr, spark_project_config, "holdenk")
        assert "large_pr_penalty" in breakdown

    def test_score_clamped(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        """Score should be between 0.0 and 1.0."""
        pr = make_pr(number=107, title="Normal PR")
        score, _ = score_pull_request(pr, spark_project_config, "holdenk")
        assert 0.0 <= score <= 1.0


class TestHelpers:
    def test_is_likely_bot(self) -> None:
        assert _is_likely_bot("dependabot[bot]") is True
        assert _is_likely_bot("renovate") is True
        assert _is_likely_bot("alice-dev") is False

    @pytest.mark.parametrize(
        ("files", "watched", "expected"),
        [
            (["sql/catalyst/a.scala", "core/b.scala"], ["sql/catalyst/"], 0.5),
            ([], ["sql/"], 0.0),
            (["src/a.py", "src/b.py"], ["src/"], 1.0),
        ],
        ids=["partial_match", "empty_files", "full_match"],
    )
    def test_path_overlap_score(
        self, files: list[str], watched: list[str], expected: float
    ) -> None:
        assert _path_overlap_score(files, watched) == expected
