"""Tests for the Snowflake code review CLI integration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import RemoteExecutionConfig, SnowflakeReviewConfig
from franktheunicorn.core.models import Project, PullRequest
from franktheunicorn.review.snowflake_review import (
    SnowflakeFinding,
    create_drafts_from_snowflake,
    run_snowflake_review,
)
from franktheunicorn.review.tool_executor import ExecResult
from tests.factories import AntiPatternFactory

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _config(**overrides: Any) -> SnowflakeReviewConfig:
    return SnowflakeReviewConfig(enabled=True, **overrides)


# ---------------------------------------------------------------------------
# Runner tests
# ---------------------------------------------------------------------------


class TestRunSnowflakeReview:
    def test_parses_findings_from_fixture(self) -> None:
        executor = MagicMock()
        executor.run.return_value = ExecResult(
            returncode=0,
            stdout=(FIXTURES_DIR / "snowflake_review_output.txt").read_text(),
            stderr="",
        )
        findings = run_snowflake_review(
            cwd="/tmp/repo",
            base_commit="origin/main",
            config=_config(),
            executor=executor,
        )
        assert len(findings) == 3
        assert findings[0].severity == "critical"
        assert findings[0].file_path == "src/etl/loader.py"
        assert "warehouse query" in findings[0].title.lower()
        assert findings[1].file_path == "src/etl/transform.py"
        assert findings[2].file_path == "dags/refresh_orders.py"

    def test_cli_command_shape(self) -> None:
        executor = MagicMock()
        executor.run.return_value = ExecResult(returncode=0, stdout="", stderr="")
        run_snowflake_review(
            cwd="/tmp/repo",
            base_commit="abc123",
            config=_config(extra_args=["--config", "snow.yaml"]),
            executor=executor,
        )
        call = executor.run.call_args
        cmd = call.kwargs.get("cmd") or call.args[0]
        assert cmd[0] == "snowflake-code-review"
        assert "review" in cmd
        assert "--prompt-only" in cmd
        assert "abc123" in cmd
        assert "--config" in cmd and "snow.yaml" in cmd

    def test_executor_failure_returns_empty(self) -> None:
        executor = MagicMock()
        executor.run.return_value = None
        assert (
            run_snowflake_review(
                cwd="/tmp/repo",
                base_commit="origin/main",
                config=_config(),
                executor=executor,
            )
            == []
        )

    def test_nonzero_exit_returns_empty(self) -> None:
        executor = MagicMock()
        executor.run.return_value = ExecResult(returncode=2, stdout="", stderr="bad config")
        assert (
            run_snowflake_review(
                cwd="/tmp/repo",
                base_commit="origin/main",
                config=_config(),
                executor=executor,
            )
            == []
        )

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_default_executor_is_local(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Review completed", stderr=""
        )
        findings = run_snowflake_review(
            cwd="/tmp/repo",
            base_commit="origin/main",
            config=_config(),
        )
        assert findings == []
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# Draft creation tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateDraftsFromSnowflake:
    def test_attribution_is_snowflake_review(self, db_pr: PullRequest) -> None:
        findings = [
            SnowflakeFinding(
                file_path="src/etl/loader.py",
                line_number=84,
                severity="critical",
                title="Unbounded query",
                body="Unbounded query\n\nDetails.",
                suggestion="Bind as parameter.",
            )
        ]
        drafts = create_drafts_from_snowflake(db_pr, findings)
        assert len(drafts) == 1
        assert drafts[0].sources == ["snowflake-review"]
        assert drafts[0].confidence == pytest.approx(0.9)

    def test_anti_pattern_suppression(self, db_pr: PullRequest, db_project: Project) -> None:
        AntiPatternFactory(pattern_text="cartesian join", project=db_project)
        findings = [
            SnowflakeFinding("a.sql", 1, "high", "Cartesian", "Has a cartesian join."),
            SnowflakeFinding("b.sql", 2, "critical", "Bug", "Real issue here."),
        ]
        drafts = create_drafts_from_snowflake(db_pr, findings, project=db_project)
        assert len(drafts) == 1
        assert drafts[0].file_path == "b.sql"


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------


class TestSnowflakeRemoteConfig:
    def test_default_remote_is_local(self) -> None:
        cfg = SnowflakeReviewConfig()
        assert cfg.remote.mode == "local"

    def test_ssh_mode_requires_host(self) -> None:
        with pytest.raises(ValueError, match="host is required"):
            RemoteExecutionConfig(mode="ssh", host="")

    def test_ssh_mode_accepts_host(self) -> None:
        cfg = RemoteExecutionConfig(mode="ssh", host="review.example.com")
        assert cfg.host == "review.example.com"

    def test_ssh_mode_no_host_allowed_with_custom_command(self) -> None:
        # Wrapper commands like "sf workspace ssh" handle routing internally.
        cfg = RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        assert cfg.host == ""
        assert cfg.ssh_command == ["sf", "workspace", "ssh"]


class TestSnowflakeCliArgv:
    def test_plain_binary(self) -> None:
        cfg = SnowflakeReviewConfig(cli_path="snowflake-code-review")
        assert cfg.cli_argv == ["snowflake-code-review"]

    def test_wrapper_command_splits(self) -> None:
        cfg = SnowflakeReviewConfig(cli_path="docker run --rm myorg/snowflake-cli")
        assert cfg.cli_argv == ["docker", "run", "--rm", "myorg/snowflake-cli"]

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="cli_path"):
            SnowflakeReviewConfig(cli_path="")
