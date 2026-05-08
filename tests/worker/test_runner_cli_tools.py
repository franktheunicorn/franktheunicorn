"""Tests for the worker's CLI-tool helpers (clone URL, PR-head checkout)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import (
    CodeRabbitConfig,
    ForgeRegistryEntry,
    OperatorConfig,
    ProjectConfig,
    RemoteExecutionConfig,
)
from franktheunicorn.core.models import PullRequest
from franktheunicorn.review.tool_executor import ExecResult
from franktheunicorn.worker.runner import (
    _checkout_pr_head,
    _clone_url_for_project,
    _run_coderabbit_for_pr,
)


def _project(forge: str = "github", owner: str = "acme", repo: str = "widget") -> ProjectConfig:
    return ProjectConfig(owner=owner, repo=repo, forge=forge)


def _operator(*forges: ForgeRegistryEntry) -> OperatorConfig:
    return OperatorConfig(
        github_token="x",
        github_username="frank",
        forges=list(forges),
    )


# ---------------------------------------------------------------------------
# _clone_url_for_project
# ---------------------------------------------------------------------------


class TestCloneUrlForProject:
    def test_github_default_returns_empty_so_template_applies(self) -> None:
        """Default github → return ""; ``clone_url_template`` (default
        ``https://github.com/{owner}/{repo}.git``) does the work, and any
        operator override of the template still applies."""
        op = _operator(
            ForgeRegistryEntry(name="github", type="github", token="x"),
        )
        assert _clone_url_for_project(_project(), op) == ""

    def test_github_enterprise_strips_api_v3(self) -> None:
        op = _operator(
            ForgeRegistryEntry(
                name="github",
                type="github",
                base_url="https://github.example.com/api/v3",
                token="x",
            ),
        )
        url = _clone_url_for_project(_project(), op)
        assert url == "https://github.example.com/acme/widget.git"

    def test_gitlab_web_url(self) -> None:
        op = _operator(
            ForgeRegistryEntry(
                name="gl",
                type="gitlab",
                base_url="https://gitlab.com",
                token="x",
            ),
        )
        url = _clone_url_for_project(_project(forge="gl"), op)
        assert url == "https://gitlab.com/acme/widget.git"

    def test_gitlab_api_url_is_stripped(self) -> None:
        """Operators may configure a forge with the API URL (``/api/v4``);
        we must strip it before constructing a clone URL."""
        op = _operator(
            ForgeRegistryEntry(
                name="gl",
                type="gitlab",
                base_url="https://gitlab.example.com/api/v4",
                token="x",
            ),
        )
        url = _clone_url_for_project(_project(forge="gl"), op)
        assert url == "https://gitlab.example.com/acme/widget.git"

    def test_self_hosted_gitea(self) -> None:
        op = _operator(
            ForgeRegistryEntry(
                name="gitea-self",
                type="gitea",
                base_url="https://git.example.com",
                token="x",
            ),
        )
        url = _clone_url_for_project(_project(forge="gitea-self"), op)
        assert url == "https://git.example.com/acme/widget.git"

    def test_gitea_api_url_is_stripped(self) -> None:
        op = _operator(
            ForgeRegistryEntry(
                name="gitea-self",
                type="gitea",
                base_url="https://git.example.com/api/v1",
                token="x",
            ),
        )
        url = _clone_url_for_project(_project(forge="gitea-self"), op)
        assert url == "https://git.example.com/acme/widget.git"

    def test_forgejo_codeberg(self) -> None:
        op = _operator(
            ForgeRegistryEntry(
                name="codeberg",
                type="forgejo",
                base_url="https://codeberg.org",
                token="x",
            ),
        )
        url = _clone_url_for_project(_project(forge="codeberg"), op)
        assert url == "https://codeberg.org/acme/widget.git"

    def test_no_operator_config_returns_empty(self) -> None:
        """No forge data → let the executor's template default apply."""
        assert _clone_url_for_project(_project(), None) == ""

    def test_unknown_forge_name_returns_empty(self) -> None:
        op = _operator(
            ForgeRegistryEntry(name="github", type="github", token="x"),
        )
        assert _clone_url_for_project(_project(forge="nonexistent"), op) == ""


# ---------------------------------------------------------------------------
# _checkout_pr_head
# ---------------------------------------------------------------------------


class TestCheckoutPrHead:
    def _pr(self, head_sha: str = "abc1234567890") -> PullRequest:
        pr = MagicMock(spec=PullRequest)
        pr.number = 42
        pr.head_sha = head_sha
        return pr

    def test_runs_fetch_then_checkout(self) -> None:
        executor = MagicMock()
        executor.run.side_effect = [
            ExecResult(returncode=0, stdout="", stderr=""),  # fetch
            ExecResult(returncode=0, stdout="", stderr=""),  # checkout
        ]
        ok = _checkout_pr_head(executor, "/srv/repo", self._pr("deadbeef"))
        assert ok is True
        assert executor.run.call_count == 2
        first_cmd = executor.run.call_args_list[0].args[0]
        second_cmd = executor.run.call_args_list[1].args[0]
        assert first_cmd[:3] == ["git", "fetch", "--quiet"]
        assert "deadbeef" in first_cmd
        assert second_cmd[:4] == ["git", "checkout", "--quiet", "--detach"]
        assert "deadbeef" in second_cmd

    def test_skips_when_head_sha_empty(self) -> None:
        executor = MagicMock()
        ok = _checkout_pr_head(executor, "/srv/repo", self._pr(head_sha=""))
        assert ok is False
        executor.run.assert_not_called()

    def test_returns_false_on_fetch_failure(self) -> None:
        executor = MagicMock()
        executor.run.side_effect = [
            ExecResult(returncode=128, stdout="", stderr="not found"),
        ]
        assert _checkout_pr_head(executor, "/srv/repo", self._pr()) is False
        # Should have stopped after the failed fetch.
        assert executor.run.call_count == 1

    def test_returns_false_on_executor_error(self) -> None:
        executor = MagicMock()
        executor.run.side_effect = [None]  # ssh missing or timeout
        assert _checkout_pr_head(executor, "/srv/repo", self._pr()) is False

    def test_returns_false_on_checkout_failure(self) -> None:
        executor = MagicMock()
        executor.run.side_effect = [
            ExecResult(returncode=0, stdout="", stderr=""),  # fetch ok
            ExecResult(returncode=1, stdout="", stderr="conflict"),  # checkout fails
        ]
        assert _checkout_pr_head(executor, "/srv/repo", self._pr()) is False


# ---------------------------------------------------------------------------
# _resolve_cwd_for_tool — remote integration via _run_coderabbit_for_pr
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRemoteCloneUsesProjectForgeUrl:
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_remote_clone_url_comes_from_project_forge(
        self,
        mock_run: Any,
        db_pr: PullRequest,
        tmp_path: Path,
    ) -> None:
        # All ssh calls return success with the CodeRabbit fixture so the
        # full remote pipeline (clone -> fetch base -> checkout PR head ->
        # run CLI) completes.
        fixture = (Path(__file__).parent.parent / "fixtures" / "coderabbit_output.txt").read_text()
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=fixture, stderr=""
        )

        db_pr.head_sha = "deadbeef" * 5
        db_pr.save()

        config = CodeRabbitConfig(
            enabled=True,
            cli_path="coderabbit",
            remote=RemoteExecutionConfig(
                mode="ssh",
                host="review.example.com",
                user="frank",
                remote_workspace_dir="/srv/frank",
            ),
        )
        # Pass a forge-derived clone URL (gitea, in this case) — the worker
        # is the one that computes this in production.
        _run_coderabbit_for_pr(
            db_pr,
            config,
            repo_path=None,  # remote mode ignores the local path
            clone_url="https://git.example.com/acme/widget.git",
        )

        # Find the prepare_repo SSH invocation. Its argv contains the
        # remote shell script which must reference the gitea clone URL,
        # not github.com.
        prepare_calls = [
            call
            for call in mock_run.call_args_list
            if "git clone" in (call.args[0][-1] if call.args else "")
        ]
        assert prepare_calls, "expected at least one prepare_repo SSH call"
        script = prepare_calls[0].args[0][-1]
        assert "git.example.com/acme/widget.git" in script
        assert "github.com" not in script
