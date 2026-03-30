"""Tests for the interest scoring orchestrator."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Project, PullRequest
from franktheunicorn.scoring.scorer import score_pull_request, score_pull_request_from_model

_NEXT_ID = iter(range(2001, 9999))


def _make_pr(db_project: Project, **kwargs: object) -> PullRequest:
    """Create a PR with sensible defaults, auto-incrementing IDs."""
    gid = next(_NEXT_ID)
    defaults: dict[str, object] = {
        "project": db_project,
        "github_id": gid,
        "number": gid,
        "title": "test",
        "author": "someone",
        "url": "https://example.com",
        "changed_files": ["README.md"],
    }
    defaults.update(kwargs)
    return PullRequest.objects.create(**defaults)


@pytest.mark.django_db
class TestScoring:
    def test_operator_is_author(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = _make_pr(db_project, author="holdenk")
        _score, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "operator_is_author" in bd
        assert _score > 0
        assert "new_contributor" not in bd

    def test_review_requested(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = _make_pr(db_project, requested_reviewers=["holdenk"])
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "review_requested" in bd

    def test_path_overlap(self, db_project: Project, spark_project_config: ProjectConfig) -> None:
        pr = _make_pr(
            db_project,
            changed_files=["sql/catalyst/rules/Opt.scala", "sql/catalyst/trees/Tree.scala"],
        )
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "path_overlap" in bd

    def test_frequent_contributor(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = _make_pr(db_project, author="cloud-fan")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "frequent_contributor" in bd
        assert "new_contributor" not in bd

    def test_new_contributor(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = _make_pr(db_project, author="brand-new-person")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "new_contributor" in bd

    def test_returning_contributor_not_new(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        _make_pr(db_project, author="returning-person")
        pr = _make_pr(db_project, author="returning-person")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "new_contributor" not in bd

    def test_bot_penalty(self, db_project: Project, spark_project_config: ProjectConfig) -> None:
        pr = _make_pr(db_project, author="dependabot[bot]")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "ai_generated_penalty" in bd

    def test_large_pr_penalty(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = _make_pr(db_project, additions=400, deletions=200)
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "large_pr_penalty" in bd

    def test_score_clamped(self, db_project: Project, spark_project_config: ProjectConfig) -> None:
        pr = _make_pr(db_project)
        score, _ = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert 0.0 <= score <= 1.0


_ALICE_PR: dict[str, object] = {
    "author": "alice",
    "requested_reviewers": [],
    "changed_files": [],
    "additions": 0,
    "deletions": 0,
}


class TestPureFunctionOrchestrator:
    def test_basic_scoring_with_dicts(self) -> None:
        pr = {
            "author": "holdenk",
            "requested_reviewers": [],
            "changed_files": ["README.md"],
            "additions": 10,
            "deletions": 5,
        }
        score, bd = score_pull_request(
            pr, {"watched_paths": [], "frequent_contributors": []}, "holdenk"
        )
        assert "operator_is_author" in bd
        assert 0.0 <= score <= 1.0

    def test_blame_data_integrated(self) -> None:
        _, bd = score_pull_request(
            {**_ALICE_PR, "changed_files": ["a.py"], "additions": 10},
            {},
            "holdenk",
            blame_data=[{"file_path": "a.py", "authors": ["holdenk"]}],
        )
        assert "blame_proximity" in bd

    def test_collaborator_integrated(self) -> None:
        history = [{"author": "alice", "reviewer": "holdenk"}] * 3
        _, bd = score_pull_request(_ALICE_PR, {}, "holdenk", review_history=history)
        assert "collaborator" in bd

    def test_custom_expression_integrated(self) -> None:
        _, bd = score_pull_request(_ALICE_PR, {}, "holdenk", custom_expressions=["0.05"])
        assert bd["custom_0"] == 0.05

    def test_optional_data_graceful(self) -> None:
        score, _ = score_pull_request(_ALICE_PR, {}, "holdenk")
        assert 0.0 <= score <= 1.0
