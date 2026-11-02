"""Tests for the deterministic relevance gates on LLM sub-checks.

A docs-only PR cannot contain a buffer overflow, so the security checks must
not spend an LLM call hunting for one. The gate is decided from
``changed_files`` alone — no network, no model — and unknown data (an empty
file list) must run everything rather than skip on a data gap.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
from franktheunicorn.core.models import PullRequest
from franktheunicorn.review.checks import _get_registry, run_enabled_checks
from franktheunicorn.review.checks.relevance import (
    CHECK_REQUIRED_CATEGORIES,
    categorize_path,
    check_applies,
)


class TestCategorizePath:
    @pytest.mark.parametrize(
        "path",
        [
            "python/pyspark/sql.py",
            "core/src/main/scala/Foo.scala",
            "src/main.cpp",
            "pkg/server.go",
            "lib.rs",
            "shell/entrypoint.sh",
        ],
    )
    def test_source_files_are_code(self, path: str) -> None:
        assert categorize_path(path) == "code"

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "docs/contributing.md",
            "python/docs/index.rst",
            "LICENSE.txt",
        ],
    )
    def test_prose_files_are_docs(self, path: str) -> None:
        assert categorize_path(path) == "docs"

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            "pom.xml",
            "requirements.txt",
            "Dockerfile",
            "python/requirements.txt",
            "build.gradle",
            "data/telemetry.json",
        ],
    )
    def test_build_and_config_files_are_config(self, path: str) -> None:
        assert categorize_path(path) == "config"

    def test_workflow_prefix_wins_over_extension_ambiguity(self) -> None:
        # Even a markdown-ish workflow-adjacent path under workflows/ counts.
        assert categorize_path(".github/workflows/README.md") == "config"

    def test_unknown_files_are_other(self) -> None:
        assert categorize_path("docs/logo.png") == "other"
        assert categorize_path("") == "other"


class TestCheckApplies:
    def test_security_check_skips_docs_only(self) -> None:
        assert check_applies("security", ["README.md", "docs/guide.md"]) is False

    def test_security_check_runs_on_code(self) -> None:
        assert check_applies("security", ["python/pyspark/sql.py"]) is True

    def test_security_check_runs_on_config(self) -> None:
        # Dependency and workflow changes are a real security surface.
        assert check_applies("security", ["pom.xml"]) is True
        assert check_applies("security", [".github/workflows/ci.yml"]) is True

    def test_docs_plus_unknown_still_skips_security(self) -> None:
        assert check_applies("security", ["README.md", "docs/logo.png"]) is False

    def test_empty_file_list_means_unknown_and_runs(self) -> None:
        assert check_applies("security", []) is True

    def test_pr_metadata_checks_always_apply(self) -> None:
        for name in ("pr-description", "issue-link", "malicious-prompt"):
            assert check_applies(name, ["README.md"]) is True

    def test_doc_accuracy_keeps_docs_only_prs(self) -> None:
        assert check_applies("doc-accuracy", ["docs/guide.md"]) is True
        assert check_applies("doc-accuracy", ["pom.xml"]) is False

    def test_every_registered_check_has_a_gate_decision(self) -> None:
        """A new check must consciously choose a category set or always-run."""
        always_run = {"pr-description", "issue-link", "malicious-prompt"}
        registry = _get_registry()
        assert set(CHECK_REQUIRED_CATEGORIES) <= set(registry)
        assert set(registry) <= set(CHECK_REQUIRED_CATEGORIES) | always_run


@pytest.mark.django_db
class TestRunEnabledChecksGating:
    def _run(self, db_pr: PullRequest, changed_files: list[str]) -> list[str]:
        db_pr.changed_files = changed_files
        db_pr.save()
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["security", "api-misuse", "pr-description"],
        )
        op_config = OperatorConfig(llm_backends=[LLMBackendConfig(provider="stub")])
        ran: list[str] = []

        def fake(check: object, *args: Any, **kwargs: Any) -> list[Any]:
            ran.append(check.name)
            return []

        with patch("franktheunicorn.review.checks._run_check_dispatch", side_effect=fake):
            run_enabled_checks(db_pr, "diff", project_config=config, operator_config=op_config)
        return ran

    def test_docs_only_pr_runs_only_metadata_checks(self, db_pr: PullRequest) -> None:
        ran = self._run(db_pr, ["README.md", "docs/contributing.md"])
        assert ran == ["pr-description"]

    def test_code_pr_runs_everything(self, db_pr: PullRequest) -> None:
        ran = self._run(db_pr, ["python/pyspark/sql.py"])
        assert ran == ["security", "api-misuse", "pr-description"]

    def test_unknown_files_run_everything(self, db_pr: PullRequest) -> None:
        ran = self._run(db_pr, [])
        assert ran == ["security", "api-misuse", "pr-description"]

    def test_skipped_checks_leave_no_run_record(self, db_pr: PullRequest) -> None:
        """ "Not run" must not read as "ran clean" on the dashboard."""
        self._run(db_pr, ["README.md"])
        db_pr.refresh_from_db()
        runs = db_pr.agent_runs or {}
        assert "check:security" not in runs
        assert "check:api-misuse" not in runs
        assert runs["check:pr-description"]["status"] == "ok"

    def test_skip_logs_why_at_info(
        self, db_pr: PullRequest, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="franktheunicorn.review.checks"):
            self._run(db_pr, ["README.md"])
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("'security' skipped" in m for m in messages)
