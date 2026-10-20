"""Tests for CI-failure analysis (scoring/ci_analysis.py) and the signals it
feeds into merge_readiness.flake_confidence / classify_ci."""

from __future__ import annotations

from typing import Any

import pytest

from franktheunicorn.scoring.ci_analysis import analyze_pr_ci, base_failing_checks
from franktheunicorn.scoring.merge_readiness import (
    CI_FLAKY,
    CI_RED,
    CICheck,
    classify_ci,
    current_ci_analysis,
    flake_confidence,
)
from tests.factories import ProjectFactory, PullRequestFactory


def _fail(name: str) -> dict[str, str]:
    return {"name": name, "status": "completed", "conclusion": "failure"}


def _pass(name: str) -> dict[str, str]:
    return {"name": name, "status": "completed", "conclusion": "success"}


class _FakeClient:
    def __init__(
        self, base_checks: list[dict[str, str]], outputs: dict[str, str] | None = None
    ) -> None:
        self._base_checks = base_checks
        self._outputs = outputs or {}
        self.raise_checks = False

    def get_commit_checks(self, owner: str, repo: str, sha: str) -> list[dict[str, str]]:
        if self.raise_checks:
            raise RuntimeError("boom")
        return self._base_checks

    def get_commit_check_outputs(self, owner: str, repo: str, sha: str) -> dict[str, str]:
        return self._outputs


# -- flake_confidence: the two new signals ---------------------------------


class TestFlakeConfidenceSignals:
    def test_base_failing_is_strong_flake_prior(self) -> None:
        conf, why = flake_confidence(CICheck(name="pyspark"), base_failing={"pyspark"})
        assert conf >= 0.85
        assert "base branch" in why

    def test_base_failing_blends_with_history(self) -> None:
        # Operator has repeatedly said this is real; it should pull the
        # base-failing prior down rather than be ignored.
        conf, _ = flake_confidence(
            CICheck(name="pyspark"),
            base_failing={"pyspark"},
            history={"pyspark": (0, 8)},
        )
        assert conf < 0.85

    def test_llm_real_pr_verdict_overrides_to_low(self) -> None:
        conf, why = flake_confidence(
            CICheck(name="pyspark"),
            base_failing={"pyspark"},  # even with base-failing, the LLM wins
            analysis={"pyspark": {"verdict": "real-pr", "reason": "touches changed code"}},
        )
        assert conf < 0.1
        assert "touches changed code" in why

    def test_llm_flake_verdict_overrides_to_high(self) -> None:
        conf, _ = flake_confidence(
            CICheck(name="x"),
            analysis={"x": {"verdict": "flake", "reason": "network blip"}},
        )
        assert conf >= 0.9


class TestClassifyCIWithSignals:
    def test_base_failing_makes_a_lone_failure_flaky(self) -> None:
        # One check fails, and it is also failing on base → flaky, not red.
        a = classify_ci([_pass("a"), _fail("flaky-check")], base_failing={"flaky-check"})
        assert a.status == CI_FLAKY

    def test_real_pr_verdict_keeps_it_red(self) -> None:
        a = classify_ci(
            [_fail("build")],
            analysis={"build": {"verdict": "real-pr", "reason": "compile error in changed file"}},
        )
        assert a.status == CI_RED


# -- base_failing_checks ----------------------------------------------------


class TestBaseFailingChecks:
    def test_returns_failing_names(self) -> None:
        client = _FakeClient([_fail("pyspark"), _pass("lint")])
        assert base_failing_checks(client, "apache", "spark", "abc") == {"pyspark"}

    def test_empty_base_sha(self) -> None:
        client = _FakeClient([_fail("pyspark")])
        assert base_failing_checks(client, "apache", "spark", "") == set()

    def test_fetch_failure_is_empty(self) -> None:
        client = _FakeClient([_fail("pyspark")])
        client.raise_checks = True
        assert base_failing_checks(client, "apache", "spark", "abc") == set()


# -- analyze_pr_ci (Layer 1 + Layer 2) --------------------------------------


@pytest.mark.django_db
class TestAnalyzePrCI:
    def _pr(self, checks: list[dict[str, str]], head: str = "head1") -> Any:
        project = ProjectFactory(owner="apache", repo="spark")
        return PullRequestFactory(
            project=project,
            head_sha=head,
            base_sha="base1",
            ci_checks=checks,
            ci_checks_sha=head,
        )

    def test_no_failing_checks_clears_analysis(self) -> None:
        pr = self._pr([_pass("a")])
        out = analyze_pr_ci(pr, _FakeClient([]))
        assert out == {}
        assert pr.ci_analysis == {}

    def test_base_comparison_marks_pre_existing(self) -> None:
        pr = self._pr([_fail("pyspark"), _fail("sql")])
        client = _FakeClient([_fail("pyspark")])  # only pyspark fails on base
        out = analyze_pr_ci(pr, client)
        assert out["sha"] == "head1"
        assert out["base_failing"] == ["pyspark"]
        assert out["verdicts"]["pyspark"]["verdict"] == "real-pre-existing"
        assert out["verdicts"]["pyspark"]["source"] == "base"
        # "sql" is not failing on base and the LLM layer is off → no verdict.
        assert "sql" not in out["verdicts"]

    def test_current_ci_analysis_round_trips(self) -> None:
        pr = self._pr([_fail("pyspark")])
        analyze_pr_ci(pr, _FakeClient([_fail("pyspark")]))
        base, verdicts = current_ci_analysis(pr)
        assert base == {"pyspark"}
        assert verdicts["pyspark"]["verdict"] == "real-pre-existing"

    def test_current_ci_analysis_sha_guard(self) -> None:
        pr = self._pr([_fail("pyspark")])
        analyze_pr_ci(pr, _FakeClient([_fail("pyspark")]))
        pr.head_sha = "moved"  # analysis was for head1
        base, verdicts = current_ci_analysis(pr)
        assert base == set()
        assert verdicts == {}

    def test_llm_layer_adds_verdict_for_unexplained_check(self) -> None:
        pr = self._pr([_fail("sql")])

        class _Backend:
            def metered_call(self, *a: Any, **k: Any) -> str:
                return '{"verdict": "real-pr", "reason": "compile error in the changed file"}'

        # get_backend is imported lazily inside analyze_pr_ci from this module.
        from unittest.mock import patch

        from franktheunicorn.config.models import LLMBackendConfig

        with patch("franktheunicorn.review.backends.get_backend", return_value=_Backend()):
            out = analyze_pr_ci(
                pr,
                _FakeClient([], outputs={"sql": "error: type mismatch"}),
                backend_config=LLMBackendConfig(provider="openai", model="qwen3-8b"),
                use_llm=True,
                diff="+ changed code",
            )
        assert out["verdicts"]["sql"]["verdict"] == "real-pr"
        assert out["verdicts"]["sql"]["source"] == "llm"
