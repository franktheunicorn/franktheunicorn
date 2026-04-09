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
