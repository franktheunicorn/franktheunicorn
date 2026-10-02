"""Tests for the LLM sub-check registry and runner."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
from franktheunicorn.core.models import PullRequest
from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.checks import (
    BaseCheck,
    _get_registry,
    run_enabled_checks,
)
from franktheunicorn.review.prior_comments import PriorComment
from tests.factories import AntiPatternFactory


class TestRegistry:
    def test_coverage_registered(self) -> None:
        registry = _get_registry()
        assert "coverage" in registry

    def test_security_registered(self) -> None:
        registry = _get_registry()
        assert "security" in registry

    def test_security_context_registered(self) -> None:
        registry = _get_registry()
        assert "security-context" in registry

    def test_registry_values_are_base_check_subclasses(self) -> None:
        registry = _get_registry()
        for cls in registry.values():
            assert issubclass(cls, BaseCheck)


@pytest.mark.django_db
class TestRunEnabledChecks:
    def test_no_checks_configured_returns_empty(
        self,
        db_pr: PullRequest,
        spark_project_config: ProjectConfig,
        operator_config: OperatorConfig,
    ) -> None:
        """When llm_checks is empty, nothing runs."""
        assert spark_project_config.llm_checks == []
        drafts = run_enabled_checks(
            db_pr,
            "diff content",
            project_config=spark_project_config,
            operator_config=operator_config,
        )
        assert drafts == []

    def test_unknown_check_is_skipped(
        self,
        db_pr: PullRequest,
        operator_config: OperatorConfig,
    ) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["nonexistent_check"],
        )
        drafts = run_enabled_checks(
            db_pr,
            "diff content",
            project_config=config,
            operator_config=operator_config,
        )
        assert drafts == []

    def test_coverage_check_produces_drafts_with_stub(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Coverage check should produce correctly categorized drafts from findings."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["coverage"],
            test_expectations="tests required for all new features",
        )
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[
                ReviewFinding(
                    file_path="src/main.py",
                    line_number=10,
                    title="test-coverage: missing test",
                    body="No test for new function.",
                    confidence=0.8,
                    severity="important",
                ),
            ],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "+++ b/src/main.py\n+def new_func():\n+    pass",
                project_config=config,
                operator_config=op_config,
            )

        assert len(drafts) == 1
        assert "check:coverage" in drafts[0].sources
        assert drafts[0].file_path == "src/main.py"
        assert drafts[0].category == "test-coverage"

    def test_security_check_produces_drafts_with_stub(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Security check should produce correctly categorized drafts from findings."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["security"],
        )
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[
                ReviewFinding(
                    file_path="src/auth.py",
                    line_number=42,
                    title="security: hardcoded secret",
                    body="API key is hardcoded.",
                    confidence=0.9,
                    severity="critical",
                ),
            ],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "+++ b/src/auth.py\n+API_KEY = 'sk-1234'",
                project_config=config,
                operator_config=op_config,
            )

        assert len(drafts) == 1
        assert "check:security" in drafts[0].sources
        assert drafts[0].file_path == "src/auth.py"
        assert drafts[0].category == "security"

    def test_security_context_check_produces_drafts_with_stub(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Security-context check should produce drafts with category=security-context."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["security-context"],
        )
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[
                ReviewFinding(
                    file_path="src/middleware.py",
                    line_number=15,
                    title="security-context: CSRF middleware removed",
                    body="Removing CSRF middleware weakens security.",
                    confidence=0.85,
                    severity="critical",
                ),
            ],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "+++ b/src/middleware.py\n-CSRF_MIDDLEWARE = True",
                project_config=config,
                operator_config=op_config,
            )

        assert len(drafts) == 1
        assert "check:security-context" in drafts[0].sources
        assert drafts[0].file_path == "src/middleware.py"
        assert drafts[0].category == "security-context"

    def test_check_findings_go_through_antipattern_gating(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Findings that match an anti-pattern should be suppressed."""
        AntiPatternFactory(
            pattern_text="No test for new function",
            project=db_pr.project,
        )

        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["coverage"],
        )
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[
                ReviewFinding(
                    file_path="src/main.py",
                    line_number=10,
                    title="test-coverage: missing test",
                    body="No test for new function.",
                    confidence=0.8,
                    severity="important",
                ),
            ],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "some diff",
                project_config=config,
                operator_config=op_config,
            )

        assert drafts == []

    def test_defaults_to_stub_without_operator_config(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Should work without operator_config (falls back to stub)."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["coverage"],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "some diff",
                project_config=config,
            )

        assert drafts == []

    def test_check_failure_does_not_crash(
        self,
        db_pr: PullRequest,
        operator_config: OperatorConfig,
    ) -> None:
        """If a check raises, it's caught and other checks continue."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["coverage"],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            side_effect=RuntimeError("LLM exploded"),
        ):
            drafts = run_enabled_checks(
                db_pr,
                "some diff",
                project_config=config,
                operator_config=operator_config,
            )

        assert drafts == []


@pytest.mark.django_db
class TestChecksSeePriorComments:
    """The checks are a finding source too, so the +1 rule has to reach them.

    A ``security:`` check restating a reviewer's own point is exactly the
    second comment the feature exists to stop.
    """

    def _config(self) -> ProjectConfig:
        return ProjectConfig(owner="apache", repo="spark", llm_checks=["security"])

    def _comment(self) -> PriorComment:
        return PriorComment(
            author="cloud-fan",
            body="This path joins user input without checking it escapes the dir.",
            file_path="src/main.py",
            line=10,
            url="https://github.com/apache/spark/pull/1#discussion_r1",
        )

    def _finding(self) -> ReviewFinding:
        return ReviewFinding(
            file_path="src/main.py",
            line_number=10,
            title="security: path traversal",
            body="This path joins user input without checking it escapes the dir.",
            confidence=0.8,
            severity="important",
        )

    def test_the_prompt_carries_them(self, db_pr: PullRequest) -> None:
        op_config = OperatorConfig(llm_backends=[LLMBackendConfig(provider="stub")])
        seen: dict[str, str] = {}

        def capture(check: object, pr: object, diff: str, ctx: object, backend: object) -> list:
            seen["prior"] = ctx.prior_comments  # type: ignore[attr-defined]
            return []

        with patch("franktheunicorn.review.checks._run_single_check", side_effect=capture):
            run_enabled_checks(
                db_pr,
                "diff",
                project_config=self._config(),
                operator_config=op_config,
                prior_comments=[self._comment()],
            )

        assert "@cloud-fan" in seen["prior"]

    def test_a_restating_finding_becomes_a_plus_one(self, db_pr: PullRequest) -> None:
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
            github_username="holdenk",
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[self._finding()],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "diff",
                project_config=self._config(),
                operator_config=op_config,
                prior_comments=[self._comment()],
            )

        assert len(drafts) == 1
        assert drafts[0].comment_body.startswith("+1 @cloud-fan")

    def test_our_own_earlier_comment_drops_the_finding(self, db_pr: PullRequest) -> None:
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
            github_username="holdenk",
        )
        ours = PriorComment(
            author="holdenk",
            body="This path joins user input without checking it escapes the dir.",
            file_path="src/main.py",
            line=10,
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[self._finding()],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "diff",
                project_config=self._config(),
                operator_config=op_config,
                prior_comments=[ours],
            )

        assert drafts == []
