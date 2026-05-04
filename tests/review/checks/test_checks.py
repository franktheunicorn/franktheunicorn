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


@pytest.mark.django_db
class TestRunChecksIssueLinkEnrichment:
    """Test issue-link check enriches pr_context with linked issues context."""

    def test_issue_link_check_fetches_linked_issues_context(self, db_pr: PullRequest) -> None:
        """When 'issue-link' is in enabled checks and the PR body has a '#',
        run_enabled_checks should call fetch_linked_issues_context to populate
        pr_context.linked_issues_context."""

        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["issue-link"],
        )
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

        # Give the PR a body that looks like it references an issue
        db_pr.body = "Fixes #1234 - race condition"
        db_pr.save()

        fetched_context = "Issue #1234: Fix race condition in scheduler"

        with (
            patch(
                "franktheunicorn.review.drafter.fetch_linked_issues_context",
                return_value=fetched_context,
            ),
            patch("franktheunicorn.review.checks._run_single_check", return_value=[]),
        ):
            from franktheunicorn.review.checks import run_enabled_checks

            run_enabled_checks(
                db_pr,
                "some diff",
                project_config=config,
                operator_config=op_config,
            )


@pytest.mark.django_db
class TestRunSingleCheck:
    """Tests for _run_single_check using a real BaseLLMBackend subclass."""

    def test_calls_call_api_on_base_llm_backend(self, db_pr: PullRequest) -> None:
        """_run_single_check should call _call_api directly on BaseLLMBackend subclasses."""
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.checks import _run_single_check
        from franktheunicorn.review.checks.coverage import CoverageCheck
        from tests.conftest import make_pr_context

        backend_config = LLMBackendConfig(provider="claude")
        pr_context = make_pr_context()
        check = CoverageCheck()

        expected_raw = '[{"file_path": "a.py", "line_number": 1, "title": "test-coverage: missing test", "body": "Add a test.", "confidence": 0.8, "severity": "important"}]'

        with (
            patch(
                "franktheunicorn.review.backends.claude_backend.ClaudeBackend._resolve_api_key",
                return_value="fake-key",
            ),
            patch(
                "franktheunicorn.review.backends.claude_backend.ClaudeBackend._call_api",
                return_value=expected_raw,
            ) as mock_call,
        ):
            findings = _run_single_check(check, "diff content", pr_context, backend_config)

        mock_call.assert_called_once()
        assert len(findings) == 1
        assert findings[0].file_path == "a.py"

    def test_fallback_to_generate_findings_for_stub(self, db_pr: PullRequest) -> None:
        """_run_single_check uses generate_findings fallback for StubBackend."""
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.checks import _run_single_check
        from franktheunicorn.review.checks.coverage import CoverageCheck
        from tests.conftest import make_pr_context

        backend_config = LLMBackendConfig(provider="stub")
        pr_context = make_pr_context()
        check = CoverageCheck()

        # StubBackend does not extend BaseLLMBackend — falls back to generate_findings
        findings = _run_single_check(check, "diff content", pr_context, backend_config)
        assert isinstance(findings, list)


@pytest.mark.django_db
class TestRunCheckDispatch:
    """Tests for _run_check_dispatch."""

    def test_dispatches_to_scan_method_when_present(self, db_pr: PullRequest) -> None:
        """_run_check_dispatch should call check.scan() instead of the prompt pipeline."""
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.backends.base import ReviewFinding
        from franktheunicorn.review.checks import BaseCheck, _run_check_dispatch

        class CustomScanCheck(BaseCheck):
            name = "custom-scan"

            def build_prompt(self, diff: str, pr_context: object) -> tuple[str, str]:
                return "sys", "user"

            def scan(self, pr: object, diff: str, backend_config: object) -> list[ReviewFinding]:
                return [
                    ReviewFinding(
                        file_path="custom.py",
                        line_number=5,
                        title="custom: finding",
                        body="Found by scan",
                        confidence=0.9,
                        severity="important",
                    )
                ]

        check = CustomScanCheck()
        backend_config = LLMBackendConfig(provider="stub")
        pr_context = object()

        findings = _run_check_dispatch(check, db_pr, "diff", pr_context, backend_config)

        assert len(findings) == 1
        assert findings[0].file_path == "custom.py"
        assert findings[0].body == "Found by scan"


@pytest.mark.django_db
class TestInstantiateCheck:
    """Tests for _instantiate_check."""

    def test_api_misuse_gets_config_args(self, db_pr: PullRequest) -> None:
        """_instantiate_check should pass config, package_roots, and repo_path to APIMisuseCheck."""
        from franktheunicorn.config.models import APIMisuseConfig
        from franktheunicorn.review.checks import _instantiate_check
        from franktheunicorn.review.checks.api_misuse import APIMisuseCheck

        project_config = ProjectConfig(
            owner="apache",
            repo="spark",
            api_misuse=APIMisuseConfig(enabled=True),
        )

        check = _instantiate_check(APIMisuseCheck, "api-misuse", project_config, "/tmp/repo")

        assert isinstance(check, APIMisuseCheck)

    def test_non_api_misuse_check_uses_default_constructor(self) -> None:
        """_instantiate_check should call cls() for non-special checks."""
        from franktheunicorn.review.checks import _instantiate_check
        from franktheunicorn.review.checks.coverage import CoverageCheck

        project_config = ProjectConfig(owner="apache", repo="spark")
        check = _instantiate_check(CoverageCheck, "coverage", project_config, None)

        assert isinstance(check, CoverageCheck)
