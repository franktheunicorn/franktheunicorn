"""Tests for the interest scoring orchestrator (§2.1)."""

from __future__ import annotations

from typing import Any

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.scoring.scorer import score_pull_request, score_pull_request_from_model
from franktheunicorn.scoring.signals import MAX_SCORE

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
    def test_review_requested(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(requested_reviewers=["holdenk"])
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "has_review_request" in bd

    def test_path_overlap(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(changed_files=["sql/catalyst/rules/Opt.scala"])
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "path_overlap" in bd

    def test_frequent_contributor(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(author="cloud-fan")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "collaborator" in bd

    def test_new_contributor(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(author="brand-new-person")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "new_human_contributor" in bd

    def test_returning_not_new(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        make_pr(author="returning")
        pr = make_pr(author="returning")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "new_human_contributor" not in bd

    def test_bot_penalty(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr(author="dependabot[bot]")
        _, bd = score_pull_request_from_model(pr, spark_project_config, "holdenk")
        assert "ai_generated" in bd

    def test_score_normalized(self, make_pr: Any, spark_project_config: ProjectConfig) -> None:
        pr = make_pr()
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
        assert bd["custom_0"] == 15.0  # 0.5 * default max_boost 30

    def test_custom_expression_max_boost(self) -> None:
        _, bd = score_pull_request(
            _ALICE_PR, {"custom_scoring_max_boost": 10}, "holdenk", custom_expressions=["0.5"]
        )
        assert bd["custom_0"] == 5.0  # 0.5 * configured max_boost 10

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

    def test_tuple_inputs_are_coerced(self) -> None:
        tuple_pr = {**_ALICE_PR, "requested_reviewers": ("holdenk",), "assignees": ("holdenk",)}
        tuple_score, tuple_bd = score_pull_request(
            tuple_pr, {"watched_paths": ("src/",)}, "holdenk"
        )

        list_pr = {**_ALICE_PR, "requested_reviewers": ["holdenk"], "assignees": ["holdenk"]}
        list_score, list_bd = score_pull_request(list_pr, {"watched_paths": ["src/"]}, "holdenk")

        assert tuple_bd == list_bd
        assert tuple_score == list_score
