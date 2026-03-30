"""Tests for the interest scoring orchestrator."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Project, PullRequest
from franktheunicorn.scoring.scorer import score_pull_request, score_pull_request_from_model
from franktheunicorn.scoring.signals import MAX_SCORE

_NEXT_ID = iter(range(3001, 9999))


def _make_pr(db_project: Project, **kwargs: object) -> PullRequest:
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


_ALICE_PR: dict[str, object] = {
    "author": "alice",
    "requested_reviewers": [],
    "assignees": [],
    "changed_files": [],
    "additions": 0,
    "deletions": 0,
    "title": "Test PR",
    "body": "",
}


@pytest.mark.django_db
class TestScoringFromModel:
    def test_review_requested(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = _make_pr(db_project, requested_reviewers=["holdenk"])
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "has_review_request" in bd

    def test_path_overlap(self, db_project: Project, spark_project_config: ProjectConfig) -> None:
        pr = _make_pr(db_project, changed_files=["sql/catalyst/rules/Opt.scala"])
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "path_overlap" in bd

    def test_frequent_contributor(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = _make_pr(db_project, author="cloud-fan")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "collaborator" in bd

    def test_new_contributor(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = _make_pr(db_project, author="brand-new-person")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "new_human_contributor" in bd

    def test_returning_not_new(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        _make_pr(db_project, author="returning")
        pr = _make_pr(db_project, author="returning")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "new_human_contributor" not in bd

    def test_bot_penalty(self, db_project: Project, spark_project_config: ProjectConfig) -> None:
        pr = _make_pr(db_project, author="dependabot[bot]")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "ai_generated" in bd

    def test_score_normalized(
        self, db_project: Project, spark_project_config: ProjectConfig
    ) -> None:
        pr = _make_pr(db_project)
        score, _ = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert 0.0 <= score <= 1.0


class TestPureFunctionOrchestrator:
    def test_basic(self) -> None:
        pr = {**_ALICE_PR, "changed_files": ["src/a.py"]}
        score, bd = score_pull_request(pr, {"watched_paths": ["src/"]}, "holdenk")
        assert "path_overlap" in bd
        assert 0.0 <= score <= 1.0

    def test_keyword_match(self) -> None:
        pr = {**_ALICE_PR, "title": "Fix OOM in executor"}
        _, bd = score_pull_request(pr, {"watch_keywords": ["OOM"]}, "holdenk")
        assert "keyword_match" in bd

    def test_mentioned(self) -> None:
        pr = {**_ALICE_PR, "body": "cc @holdenk please review"}
        _, bd = score_pull_request(pr, {}, "holdenk")
        assert "mentioned_or_assigned" in bd

    def test_blame_integrated(self) -> None:
        blame = [{"file_path": "a.py", "authors": ["holdenk"]}]
        _, bd = score_pull_request(_ALICE_PR, {}, "holdenk", blame_data=blame)
        assert "touches_operator_code" in bd

    def test_collaborator_from_history(self) -> None:
        history = [{"author": "alice", "reviewer": "holdenk"}] * 3
        _, bd = score_pull_request(_ALICE_PR, {}, "holdenk", review_history=history)
        assert "collaborator" in bd
        assert "prior_review_history" in bd

    def test_collaborator_from_frequent(self) -> None:
        _, bd = score_pull_request(_ALICE_PR, {"frequent_contributors": ["alice"]}, "holdenk")
        assert "collaborator" in bd

    def test_llm_interest(self) -> None:
        pr = {**_ALICE_PR, "llm_interest": "high"}
        _, bd = score_pull_request(pr, {}, "holdenk")
        assert "llm_interest" in bd

    def test_custom_expression(self) -> None:
        _, bd = score_pull_request(_ALICE_PR, {}, "holdenk", custom_expressions=["0.5"])
        assert "custom_0" in bd

    def test_weight_override(self) -> None:
        pr = {**_ALICE_PR, "requested_reviewers": ["holdenk"]}
        _, bd = score_pull_request(pr, {"scoring_weights": {"has_review_request": 50}}, "holdenk")
        assert bd.get("has_review_request") == 50.0

    def test_normalization(self) -> None:
        pr = {**_ALICE_PR, "requested_reviewers": ["holdenk"]}
        score, bd = score_pull_request(pr, {}, "holdenk")
        expected = round(max(0.0, min(1.0, sum(bd.values()) / MAX_SCORE)), 4)
        assert score == expected

    def test_graceful_no_data(self) -> None:
        score, _ = score_pull_request(_ALICE_PR, {}, "holdenk")
        assert 0.0 <= score <= 1.0
