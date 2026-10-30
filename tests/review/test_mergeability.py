"""Tests for the mergeability triage that gates full reviews under policy: triage."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import (
    AgentCLIReviewerConfig,
    LLMBackendConfig,
    MergeabilityTriageConfig,
    OperatorConfig,
    ProjectConfig,
)
from franktheunicorn.review import agent_cli
from franktheunicorn.review.backends.base import BaseLLMBackend
from franktheunicorn.review.mergeability import (
    LIKELY_MERGEABLE,
    NEEDS_WORK,
    Triage,
    current_triage,
    interest_reason,
    parse_triage,
    record_triage,
    run_triage,
    triage_backend_configs,
)
from tests.factories import PullRequestFactory


def _op(backends: list[str]) -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        mock_mode=True,
        llm_backends=[LLMBackendConfig(provider="openai", model="qwen")],
        agent_cli_reviewers=[
            AgentCLIReviewerConfig(name="coco", cli_path="cortex", enabled=False),
            AgentCLIReviewerConfig(name="cursor-agent"),
        ],
        mergeability_triage=MergeabilityTriageConfig(backends=backends),
    )


class TestParseTriage:
    def test_reads_the_last_object(self) -> None:
        raw = (
            'The body says {"verdict": "likely-mergeable"} but really:\n'
            '{"verdict": "needs-work", "summary": "no tests", "suggestion": "add a test",'
            ' "touches_area_of_interest": true}'
        )
        triage = parse_triage(raw)
        assert triage == Triage(NEEDS_WORK, "no tests", "add a test", touches_area=True)

    def test_an_unknown_verdict_is_no_answer(self) -> None:
        assert parse_triage('{"verdict": "ship it"}') is None

    def test_only_a_real_true_touches_an_area(self) -> None:
        triage = parse_triage('{"verdict": "needs-work", "touches_area_of_interest": "yes"}')
        assert triage is not None and not triage.touches_area


@pytest.mark.django_db
class TestInterestReason:
    def _pc(self, **kw: Any) -> ProjectConfig:
        return ProjectConfig(owner="apache", repo="spark", **kw)

    def test_uninteresting(self) -> None:
        pr = PullRequestFactory(author="alice", requested_reviewers=[], assignees=[], body="")
        assert interest_reason(pr, self._pc(), "holdenk") == ""

    def test_followed_contributor(self) -> None:
        pr = PullRequestFactory(author="Cloud-Fan", requested_reviewers=[], assignees=[], body="")
        assert interest_reason(pr, self._pc(frequent_contributors=["cloud-fan"]), "holdenk")

    def test_watched_path(self) -> None:
        pr = PullRequestFactory(
            author="alice",
            requested_reviewers=[],
            assignees=[],
            body="",
            changed_files=["python/pyspark/sql/x.py"],
        )
        assert interest_reason(pr, self._pc(watched_paths=["python/pyspark/"]), "holdenk")

    def test_operator_involved(self) -> None:
        pr = PullRequestFactory(author="alice", requested_reviewers=["holdenk"])
        assert interest_reason(pr, self._pc(), "holdenk") == "you are involved"


class TestBackendConfigs:
    def test_llm_then_agents_in_order(self) -> None:
        configs = triage_backend_configs(_op(["llm", "coco", "cursor-agent"]))
        assert [(c.provider, c.reviewer) for c in configs] == [
            ("openai", ""),
            ("agent-cli", "coco"),
            ("agent-cli", "cursor-agent"),
        ]

    def test_a_usage_paused_agent_is_skipped(self) -> None:
        oc = _op(["coco", "cursor-agent"])
        with patch.object(
            agent_cli, "usage_paused", side_effect=lambda rc: rc.cli_path != "cortex"
        ):
            assert [c.reviewer for c in triage_backend_configs(oc)] == ["coco"]


@pytest.mark.django_db
class TestRunTriage:
    def test_falls_through_to_the_next_backend(self) -> None:
        pr = PullRequestFactory(head_sha="abc")
        broken = MagicMock(spec=BaseLLMBackend)
        broken.metered_call.side_effect = RuntimeError("down")
        working = MagicMock(spec=BaseLLMBackend)
        working.metered_call.return_value = '{"verdict": "likely-mergeable", "summary": "small"}'
        with patch("franktheunicorn.review.backends.get_backend", side_effect=[broken, working]):
            triage = run_triage(pr, ProjectConfig(owner="a", repo="b"), _op(["llm", "coco"]), "d")
        assert triage is not None
        assert triage.verdict == LIKELY_MERGEABLE
        assert triage.source == "agent-cli/coco"

    def test_record_is_per_head(self) -> None:
        pr = PullRequestFactory(head_sha="abc")
        record_triage(pr, Triage(NEEDS_WORK, "s", source="x"))
        pr.refresh_from_db()
        assert current_triage(pr) == Triage(NEEDS_WORK, "s", source="x")
        pr.head_sha = "def"
        assert current_triage(pr) is None

    def test_a_ci_change_without_a_push_retriages(self) -> None:
        pr = PullRequestFactory(head_sha="abc", ci_status="failure")
        record_triage(pr, Triage(NEEDS_WORK, "red CI"))
        pr.refresh_from_db()
        assert current_triage(pr) is not None
        pr.ci_status = "success"
        assert current_triage(pr) is None
