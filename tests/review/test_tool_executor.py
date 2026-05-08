"""Tests for the local + remote-SSH tool executor."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from franktheunicorn.config.models import RemoteExecutionConfig
from franktheunicorn.review.tool_executor import (
    ExecResult,
    LocalExecutor,
    RemoteSSHExecutor,
    make_executor,
)

# ---------------------------------------------------------------------------
# LocalExecutor
# ---------------------------------------------------------------------------


class TestLocalExecutorPrepareRepo:
    def test_returns_local_path_when_present(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        cwd = LocalExecutor().prepare_repo("o", "r", local_path=repo)
        assert cwd == str(repo)

    def test_returns_none_when_path_missing(self, tmp_path: Path) -> None:
        cwd = LocalExecutor().prepare_repo("o", "r", local_path=tmp_path / "missing")
        assert cwd is None

    def test_returns_none_when_path_is_none(self) -> None:
        assert LocalExecutor().prepare_repo("o", "r", local_path=None) is None


class TestLocalExecutorRun:
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_run_passes_through_subprocess(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="hi\n", stderr=""
        )
        result = LocalExecutor().run(["echo", "hi"], cwd="/tmp")
        assert result is not None
        assert result.ok
        assert result.stdout == "hi\n"
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["cwd"] == "/tmp"
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_run_forwards_stdin(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        LocalExecutor().run(["cat"], cwd="/tmp", stdin="hello")
        assert mock_run.call_args.kwargs["input"] == "hello"

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_run_returns_none_on_file_not_found(self, mock_run: Any) -> None:
        mock_run.side_effect = FileNotFoundError("no such binary")
        assert LocalExecutor().run(["nope"], cwd="/tmp") is None

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_run_returns_none_on_timeout(self, mock_run: Any) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
        assert LocalExecutor().run(["x"], cwd="/tmp", timeout=1) is None


# ---------------------------------------------------------------------------
# RemoteSSHExecutor
# ---------------------------------------------------------------------------


def _ssh_config(**overrides: Any) -> RemoteExecutionConfig:
    base: dict[str, Any] = {
        "mode": "ssh",
        "host": "review.example.com",
        "user": "frank",
        "remote_workspace_dir": "/srv/frank",
    }
    base.update(overrides)
    return RemoteExecutionConfig(**base)


class TestRemoteSSHExecutorPrepareRepo:
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_clone_or_fetch_runs_under_ssh(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        cwd = executor.prepare_repo("acme", "widget")
        assert cwd == "/srv/frank/acme/widget"

        argv = mock_run.call_args.args[0]
        assert argv[0] == "ssh"
        assert "frank@review.example.com" in argv
        # The remote shell snippet should reference both branches of the
        # idempotent clone-or-fetch logic.
        joined_script = argv[-1]
        assert "git fetch" in joined_script
        assert "git clone" in joined_script
        assert "/srv/frank/acme/widget" in joined_script

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_uses_clone_url_template(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(clone_url_template="git@gitea.example.com:{owner}/{repo}.git"),
        )
        executor.prepare_repo("acme", "widget")
        joined_script = mock_run.call_args.args[0][-1]
        assert "git@gitea.example.com:acme/widget.git" in joined_script

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_returns_none_when_remote_fails(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="Permission denied"
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        assert executor.prepare_repo("acme", "widget") is None

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_returns_none_when_ssh_missing(self, mock_run: Any) -> None:
        mock_run.side_effect = FileNotFoundError("no ssh binary")
        executor = RemoteSSHExecutor(config=_ssh_config())
        assert executor.prepare_repo("acme", "widget") is None

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_tilde_workspace_expands_via_dollar_home(self, mock_run: Any) -> None:
        """``~/.frank-remote`` must be emitted as ``"$HOME"/...`` so the
        remote shell expands it instead of taking ``~`` literally
        (shlex.quote single-quotes the path otherwise)."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(remote_workspace_dir="~/.frank-remote"),
        )
        executor.prepare_repo("acme", "widget")

        script = mock_run.call_args.args[0][-1]
        # The workspace path should be emitted with $HOME unquoted (so the
        # remote shell expands it) and the suffix safely shell-quoted.
        assert '"$HOME"/.frank-remote/acme' in script
        assert '"$HOME"/.frank-remote/acme/widget' in script
        # And the literal "~" must NOT appear in single-quoted form.
        assert "'~/" not in script
        assert "'~'" not in script

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_absolute_workspace_uses_plain_quoting(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(remote_workspace_dir="/srv/frank"),
        )
        executor.prepare_repo("acme", "widget")
        script = mock_run.call_args.args[0][-1]
        assert "/srv/frank/acme/widget" in script
        assert "$HOME" not in script


class TestRemoteSSHExecutorRun:
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_run_quotes_args_for_remote_shell(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        result = executor.run(
            ["coderabbit", "review", "--base-commit", "origin/main"],
            cwd="/srv/frank/acme/widget",
        )
        assert result is not None and result.ok
        argv = mock_run.call_args.args[0]
        assert argv[0] == "ssh"
        # The last positional arg is the remote shell command. It should
        # cd into the remote dir and invoke the CLI with each argument
        # individually shell-quoted.
        remote_cmd = argv[-1]
        assert remote_cmd.startswith("cd ")
        assert "/srv/frank/acme/widget" in remote_cmd
        assert "coderabbit review --base-commit origin/main" in remote_cmd

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_run_includes_ssh_key_and_extra_args(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(
                ssh_key_path="/home/u/.ssh/frank",
                ssh_extra_args=["-o", "StrictHostKeyChecking=no"],
            ),
        )
        executor.run(["true"], cwd="/srv/frank")
        argv = mock_run.call_args.args[0]
        assert "-i" in argv and "/home/u/.ssh/frank" in argv
        assert "StrictHostKeyChecking=no" in argv

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_run_returns_none_on_timeout(self, mock_run: Any) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=1)
        executor = RemoteSSHExecutor(config=_ssh_config())
        assert executor.run(["true"], cwd="/srv/frank", timeout=1) is None

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_run_tilde_cwd_expands_via_dollar_home(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        executor.run(["true"], cwd="~/.frank-remote/acme/widget")
        remote_cmd = mock_run.call_args.args[0][-1]
        assert remote_cmd.startswith('cd "$HOME"/.frank-remote/acme/widget')
        assert "'~/" not in remote_cmd


# ---------------------------------------------------------------------------
# make_executor
# ---------------------------------------------------------------------------


class TestMakeExecutor:
    def test_local_when_none(self) -> None:
        assert isinstance(make_executor(None), LocalExecutor)

    def test_local_when_mode_local(self) -> None:
        cfg = RemoteExecutionConfig()  # default mode="local"
        assert isinstance(make_executor(cfg), LocalExecutor)

    def test_remote_when_mode_ssh(self) -> None:
        cfg = _ssh_config()
        assert isinstance(make_executor(cfg), RemoteSSHExecutor)


class TestExecResult:
    def test_ok_property(self) -> None:
        assert ExecResult(returncode=0, stdout="", stderr="").ok
        assert not ExecResult(returncode=1, stdout="", stderr="").ok
