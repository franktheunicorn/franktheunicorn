"""Tests for the Claude CLI review integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import ClaudeCLIConfig
from franktheunicorn.core.models import Project, PullRequest
from franktheunicorn.review.claude_cli import (
    ClaudeCLIFinding,
    create_drafts_from_claude_cli,
    run_claude_cli_review,
)
from franktheunicorn.review.tool_executor import ExecResult
from tests.factories import AntiPatternFactory

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _executor_returning(*results: ExecResult | None) -> MagicMock:
    """Build a mock ToolExecutor whose ``run`` yields the given results."""
    mock = MagicMock()
    mock.run.side_effect = list(results)
    return mock


# ---------------------------------------------------------------------------
# CLI runner tests (via mocked executor)
# ---------------------------------------------------------------------------


class TestRunClaudeCLIReview:
    def _config(self, **overrides: Any) -> ClaudeCLIConfig:
        return ClaudeCLIConfig(enabled=True, cli_path="claude", **overrides)

    def test_parses_findings_from_cli_output(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n+pass\n", stderr="")
        cli_result = ExecResult(
            returncode=0,
            stdout=(FIXTURES_DIR / "claude_cli_output.txt").read_text(),
            stderr="",
        )
        executor = _executor_returning(diff_result, cli_result)

        findings = run_claude_cli_review(
            cwd="/tmp/repo",
            base_commit="origin/main",
            config=self._config(),
            executor=executor,
        )
        assert len(findings) == 3
        assert findings[0].file_path == "src/auth/session.py"
        assert findings[0].severity == "high"
        assert findings[1].severity == "medium"
        assert findings[2].severity == "nit"

    def test_empty_diff_skips_cli(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="", stderr="")
        executor = _executor_returning(diff_result)
        findings = run_claude_cli_review(
            cwd="/tmp/repo",
            base_commit="origin/main",
            config=self._config(),
            executor=executor,
        )
        assert findings == []
        # Only git diff was attempted; the CLI itself was never invoked.
        assert executor.run.call_count == 1

    def test_diff_failure_returns_empty(self) -> None:
        diff_result = ExecResult(returncode=128, stdout="", stderr="not a git repo")
        executor = _executor_returning(diff_result)
        assert (
            run_claude_cli_review(
                cwd="/tmp/repo",
                base_commit="origin/main",
                config=self._config(),
                executor=executor,
            )
            == []
        )

    def test_cli_not_available_returns_empty(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n", stderr="")
        executor = _executor_returning(diff_result, None)
        assert (
            run_claude_cli_review(
                cwd="/tmp/repo",
                base_commit="origin/main",
                config=self._config(),
                executor=executor,
            )
            == []
        )

    def test_cli_nonzero_exit_returns_empty(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n", stderr="")
        cli_result = ExecResult(returncode=1, stdout="", stderr="rate limit")
        executor = _executor_returning(diff_result, cli_result)
        assert (
            run_claude_cli_review(
                cwd="/tmp/repo",
                base_commit="origin/main",
                config=self._config(),
                executor=executor,
            )
            == []
        )

    def test_diff_truncated_when_oversize(self) -> None:
        big_diff = "+x\n" * 50_000  # 150_000 chars
        diff_result = ExecResult(returncode=0, stdout=big_diff, stderr="")
        cli_result = ExecResult(returncode=0, stdout="Review completed", stderr="")
        executor = _executor_returning(diff_result, cli_result)

        run_claude_cli_review(
            cwd="/tmp/repo",
            base_commit="origin/main",
            config=self._config(max_diff_chars=10_000),
            executor=executor,
        )
        # Second call is the CLI invocation. Its prompt argument should
        # contain the truncation marker.
        cli_call = executor.run.call_args_list[1]
        cmd = cli_call.kwargs.get("cmd") or cli_call.args[0]
        # ``-p`` is followed by the prompt string in our argv.
        prompt = cmd[cmd.index("-p") + 1]
        assert "[...diff truncated...]" in prompt
        assert len(prompt) < len(big_diff) + 2_000  # bounded

    def test_model_flag_passed_through(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n", stderr="")
        cli_result = ExecResult(returncode=0, stdout="Review completed", stderr="")
        executor = _executor_returning(diff_result, cli_result)
        run_claude_cli_review(
            cwd="/tmp/repo",
            base_commit="origin/main",
            config=self._config(model="claude-sonnet-4-6"),
            executor=executor,
        )
        cli_call = executor.run.call_args_list[1]
        cmd = cli_call.kwargs.get("cmd") or cli_call.args[0]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_default_executor_is_local(self, mock_run: Any) -> None:
        """When no executor is supplied, the call falls through to LocalExecutor."""
        # First call: git diff returns empty so CLI is skipped.
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        findings = run_claude_cli_review(
            cwd="/tmp/repo",
            base_commit="origin/main",
            config=self._config(),
        )
        assert findings == []
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# Draft creation tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateDraftsFromClaudeCLI:
    def test_creates_drafts_with_claude_cli_source(self, db_pr: PullRequest) -> None:
        findings = [
            ClaudeCLIFinding(
                file_path="src/auth/session.py",
                line_number=57,
                severity="high",
                title="Token verified after use",
                body="Token verified after use\n\nDetails.",
                suggestion="Verify first.",
            )
        ]
        drafts = create_drafts_from_claude_cli(db_pr, findings)
        assert len(drafts) == 1
        assert drafts[0].sources == ["claude-cli"]
        assert drafts[0].suggestion == "Verify first."
        assert drafts[0].confidence == pytest.approx(0.8)

    def test_anti_pattern_suppression(self, db_pr: PullRequest, db_project: Project) -> None:
        AntiPatternFactory(pattern_text="rename to", project=db_project)
        findings = [
            ClaudeCLIFinding("a.py", 1, "nit", "Rename", "Rename to something better."),
            ClaudeCLIFinding("b.py", 2, "high", "Bug", "Real issue here."),
        ]
        drafts = create_drafts_from_claude_cli(db_pr, findings, project=db_project)
        assert len(drafts) == 1
        assert drafts[0].file_path == "b.py"

    def test_unknown_severity_falls_back_to_default_confidence(self, db_pr: PullRequest) -> None:
        findings = [ClaudeCLIFinding("a.py", 1, "blocker", "T", "T")]
        drafts = create_drafts_from_claude_cli(db_pr, findings)
        assert drafts[0].confidence == pytest.approx(0.5)


class TestClaudeCLICliArgv:
    """``cli_path`` can be a wrapper command like ``uv run claude`` so
    operators don't need to bake the CLI into their PATH."""

    def test_plain_binary(self) -> None:
        cfg = ClaudeCLIConfig(cli_path="claude")
        assert cfg.cli_argv == ["claude"]

    def test_wrapper_command_splits(self) -> None:
        cfg = ClaudeCLIConfig(cli_path="uv run claude")
        assert cfg.cli_argv == ["uv", "run", "claude"]

    def test_wrapper_propagates_to_subprocess(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n+pass\n", stderr="")
        cli_result = ExecResult(returncode=0, stdout="", stderr="")
        executor = _executor_returning(diff_result, cli_result)

        cfg = ClaudeCLIConfig(enabled=True, cli_path="corp-wrap claude --headless")
        run_claude_cli_review(cwd="/tmp/repo", base_commit="abc", config=cfg, executor=executor)

        # The second executor.run call is the actual CLI invocation.
        cli_call = executor.run.call_args_list[1]
        cmd = cli_call.args[0]
        assert cmd[:3] == ["corp-wrap", "claude", "--headless"]

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="cli_path"):
            ClaudeCLIConfig(cli_path="")
