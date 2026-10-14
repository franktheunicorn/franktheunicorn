"""Tests for the local + remote-SSH tool executor."""

from __future__ import annotations

import json
import logging
import os
import select
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import RemoteExecutionConfig
from franktheunicorn.review.tool_executor import (
    _DELIVERY_SENTINEL,
    ExecResult,
    LocalExecutor,
    RemoteSSHExecutor,
    _git_verbosity_flag,
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
    """Real processes, not a mocked ``subprocess.run``: the point of the local
    runner is how it behaves with real children, helpers and timeouts."""

    @patch("franktheunicorn.review.tool_executor._run_local")
    def test_run_goes_through_the_process_group_runner(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="hi\n", stderr=""
        )
        result = LocalExecutor().run(["echo", "hi"], cwd="/tmp", timeout=7, stdin="in")
        assert result is not None
        assert result.ok
        assert result.stdout == "hi\n"
        mock_run.assert_called_once_with(["echo", "hi"], "/tmp", 7, "in")

    def test_output_and_exit_code_come_back(self) -> None:
        result = LocalExecutor().run(["sh", "-c", "echo out; echo err >&2; exit 3"], cwd="/tmp")
        assert result is not None
        assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")

    def test_stdin_is_delivered(self) -> None:
        result = LocalExecutor().run(["cat"], cwd="/tmp", stdin="hello")
        assert result is not None
        assert result.stdout == "hello"

    def test_no_stdin_means_end_of_file_not_the_workers_stdin(self) -> None:
        """``cat`` with nothing to read must return at once, not wait on whatever
        the worker process was started with."""
        started = time.monotonic()
        result = LocalExecutor().run(["cat"], cwd="/tmp", timeout=20)
        assert result is not None
        assert result.stdout == ""
        assert time.monotonic() - started < 10

    def test_a_missing_binary_is_none(self) -> None:
        assert LocalExecutor().run(["frank-no-such-binary-xyz"], cwd="/tmp") is None

    def test_a_timeout_is_none_and_does_not_wait_out_the_child(self) -> None:
        started = time.monotonic()
        assert LocalExecutor().run(["sh", "-c", "sleep 30"], cwd="/tmp", timeout=1) is None
        assert time.monotonic() - started < 10

    def test_a_helper_holding_stdout_neither_delays_the_answer_nor_survives(
        self, tmp_path: Path
    ) -> None:
        """The agent exits with its answer while a helper it spawned still holds
        the output stream. With pipes the read waited for the helper; here the
        answer comes back at once and the helper is killed with the group."""
        pidfile = tmp_path / "helper.pid"
        script = f"sleep 30 & echo $! > {pidfile}; echo done"
        started = time.monotonic()

        result = LocalExecutor().run(["sh", "-c", script], cwd="/tmp", timeout=60)

        assert result is not None
        assert result.stdout == "done\n"
        assert time.monotonic() - started < 10
        status = Path(f"/proc/{pidfile.read_text().strip()}/status")
        if status.exists():  # killed but not yet reaped is fine; running is not
            state = next(
                line for line in status.read_text().splitlines() if line.startswith("State:")
            )
            assert "Z" in state


class TestAuthFailure:
    def test_a_login_complaint_on_stderr_is_recognised(self) -> None:
        from franktheunicorn.review.tool_executor import looks_like_auth_failure

        for line in (
            "Error: Not logged in. Please run /login",
            "Authentication required",
            "401 Unauthorized",
            "Invalid API key",
        ):
            assert looks_like_auth_failure(line), line

    def test_a_prompt_echoed_to_stderr_does_not_count(self) -> None:
        """codex prints the whole prompt to stderr before its own error; a
        report about unauthorized access is not a login failure."""
        from franktheunicorn.review.tool_executor import looks_like_auth_failure

        lines = ["user", "Report: Unauthorized access to the admin API", *(["..."] * 10)]
        stderr = "\n".join([*lines, "error: stream disconnected before completion"])
        assert not looks_like_auth_failure(stderr)
        assert looks_like_auth_failure(stderr + "\nError: Not logged in")

    def test_an_ordinary_failure_is_not(self) -> None:
        from franktheunicorn.review.tool_executor import looks_like_auth_failure

        assert not looks_like_auth_failure("error: model overloaded")
        assert not looks_like_auth_failure("")

    def test_the_advice_says_where_to_log_in(self) -> None:
        from franktheunicorn.review.tool_executor import auth_failure_advice

        remote = auth_failure_advice("cursor-agent", "cursor-agent", remote=True)
        local = auth_failure_advice("claude", "claude", remote=False)
        assert "remote host" in remote and "cursor-agent" in remote
        assert "this machine" in local and "log claude in" in local


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

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_returns_none_when_remote_fails(self, mock_run: Any, mock_sleep: Any) -> None:
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


class TestRemoteSSHExecutorCustomCommand:
    """Some companies wrap ssh in a corporate helper (corp-ssh-helper,
    teleport's tsh, etc.). ``ssh_command`` must take the place of bare
    ``ssh`` while everything else (BatchMode, key path, extra args)
    appends as before."""

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_custom_ssh_command_replaces_ssh_binary(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(ssh_command=["corp-ssh-helper"]),
        )
        executor.run(["true"], cwd="/srv/frank")
        argv = mock_run.call_args.args[0]
        assert argv[0] == "corp-ssh-helper"
        assert "ssh" not in argv[:1]  # not "ssh" anymore
        # No BatchMode: that is OpenSSH grammar and this is a wrapper. The target
        # and the command still go through, because those are the wrapper's job.
        assert "-o" not in argv
        assert "frank@review.example.com" in argv

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_custom_ssh_command_supports_multi_arg_wrapper(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(ssh_command=["tsh", "ssh", "--cluster=prod"]),
        )
        executor.run(["true"], cwd="/srv/frank")
        argv = mock_run.call_args.args[0]
        assert argv[:3] == ["tsh", "ssh", "--cluster=prod"]
        # The wrapper prefix is passed through untouched — no spliced-in OpenSSH
        # options, which `tsh ssh` would read as its own flags.
        assert "-o" not in argv
        assert "BatchMode=yes" not in argv

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_custom_ssh_command_accepts_string(self, mock_run: Any) -> None:
        """A bare string for ergonomics -- shlex would be cleaner but
        most configs come from YAML, so whitespace-split is enough."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        cfg = RemoteExecutionConfig(
            mode="ssh", host="h.example.com", ssh_command="corp-ssh-helper --quiet"
        )
        executor = RemoteSSHExecutor(config=cfg)
        executor.run(["true"], cwd="/srv/frank")
        argv = mock_run.call_args.args[0]
        assert argv[:2] == ["corp-ssh-helper", "--quiet"]

    def test_empty_ssh_command_rejected(self) -> None:
        with pytest.raises(ValueError, match="ssh_command"):
            RemoteExecutionConfig(mode="ssh", host="h", ssh_command=[])

    def test_empty_string_ssh_command_rejected(self) -> None:
        with pytest.raises(ValueError, match="ssh_command"):
            RemoteExecutionConfig(mode="ssh", host="h", ssh_command="")

    def test_default_ssh_command_is_plain_ssh(self) -> None:
        cfg = RemoteExecutionConfig(mode="ssh", host="h")
        assert cfg.ssh_command == ["ssh"]

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_custom_ssh_command_used_for_prepare_repo(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(ssh_command=["corp-ssh-helper"]),
        )
        executor.prepare_repo("acme", "widget")
        argv = mock_run.call_args.args[0]
        assert argv[0] == "corp-ssh-helper"

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_custom_ssh_command_missing_logs_binary_name(
        self, mock_run: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_run.side_effect = FileNotFoundError("no such binary")
        executor = RemoteSSHExecutor(
            config=_ssh_config(ssh_command=["corp-ssh-helper"]),
        )
        with caplog.at_level("WARNING"):
            assert executor.run(["true"], cwd="/srv/frank") is None
        assert "corp-ssh-helper" in caplog.text

    def test_wrapper_command_no_host_omits_empty_target(self) -> None:
        # When host is empty (e.g. sf workspace ssh handles routing internally),
        # _ssh_command must not append an empty string — that becomes a spurious
        # positional arg that confuses CLIs expecting exactly one arg.
        cfg = RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        executor = RemoteSSHExecutor(config=cfg)
        argv = executor._ssh_command()
        assert "" not in argv
        # And no OpenSSH options either: `-o BatchMode=yes` is ssh's grammar, not
        # a wrapper's. `sf workspace ssh` swallows it, `gcloud compute ssh` would
        # reject it, and either way it is not ours to add to someone else's CLI.
        assert argv == ["sf", "workspace", "ssh"]

    def test_openssh_still_gets_its_options(self) -> None:
        cfg = RemoteExecutionConfig(mode="ssh", host="build01", port=2222)
        argv = RemoteSSHExecutor(config=cfg)._ssh_command()

        assert argv == ["ssh", "-o", "BatchMode=yes", "-p", "2222", "build01"]

    def test_a_wrapper_still_gets_ssh_extra_args(self) -> None:
        """The operator's explicit escape hatch for flags their wrapper does take."""
        cfg = RemoteExecutionConfig(
            mode="ssh",
            ssh_command=["sf", "workspace", "ssh"],
            ssh_extra_args=["--workspace", "ws-1"],
        )
        argv = RemoteSSHExecutor(config=cfg)._ssh_command()

        assert argv == ["sf", "workspace", "ssh", "--workspace", "ws-1"]

    def test_ignored_openssh_options_are_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"], port=2222)
        with caplog.at_level("WARNING"):
            RemoteSSHExecutor(config=cfg)._ssh_command()

        assert "ignoring them" in caplog.text


class TestRemoteSSHExecutorPort:
    """``port`` populates ``-p <port>`` in the ssh argv. Zero means
    omit the flag entirely (let ssh / ~/.ssh/config pick the default)."""

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_port_emits_dash_p_flag(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(config=_ssh_config(port=2222))
        executor.run(["true"], cwd="/srv/frank")
        argv = mock_run.call_args.args[0]
        assert "-p" in argv
        idx = argv.index("-p")
        assert argv[idx + 1] == "2222"

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_default_port_omits_dash_p_flag(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(config=_ssh_config())  # port defaults to 0
        executor.run(["true"], cwd="/srv/frank")
        assert "-p" not in mock_run.call_args.args[0]

    def test_negative_port_rejected(self) -> None:
        with pytest.raises(ValueError, match="port"):
            RemoteExecutionConfig(mode="ssh", host="h", port=-1)

    def test_port_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="port"):
            RemoteExecutionConfig(mode="ssh", host="h", port=70_000)


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


class TestRemoteSSHExecutorPrepareRepoRetry:
    """Backoff and clone/fetch distinction in prepare_repo."""

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_retries_on_failure_and_returns_none(self, mock_run: Any, mock_sleep: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="op=fetch", stderr="Connection closed"
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        assert executor.prepare_repo("acme", "widget") is None
        assert mock_run.call_count == 5  # 4 delays + 1 final attempt

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_succeeds_on_second_attempt(self, mock_run: Any, mock_sleep: Any) -> None:
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="op=fetch", stderr="transient"
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="op=fetch", stderr=""),
        ]
        executor = RemoteSSHExecutor(config=_ssh_config())
        assert executor.prepare_repo("acme", "widget") == "/srv/frank/acme/widget"
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_clone_label_in_warning(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="op=clone", stderr="clone error"
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        with caplog.at_level("WARNING"):
            executor.prepare_repo("acme", "widget")
        # The operation label in warning messages should be "clone", not "fetch".
        assert any("remote git clone" in r.message for r in caplog.records)
        assert not any("remote git fetch" in r.message for r in caplog.records)

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_fetch_label_in_warning(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="op=fetch", stderr="fetch error"
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        with caplog.at_level("WARNING"):
            executor.prepare_repo("acme", "widget")
        final_warnings = [
            r for r in caplog.records if "failed" in r.message and "fetch" in r.message
        ]
        assert final_warnings

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_backoff_warning_fires_when_delay_exceeds_60s(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        # All attempts fail; the 3rd inter-attempt sleep is 60s, triggering warning.
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="op=fetch", stderr="err"
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        with caplog.at_level("WARNING"):
            executor.prepare_repo("acme", "widget")
        backoff_warnings = [r for r in caplog.records if "Backing off" in r.message]
        assert backoff_warnings, "Expected at least one 'Backing off' warning log"

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_shell_script_emits_op_markers(self, mock_run: Any, mock_sleep: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="op=fetch", stderr=""
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        executor.prepare_repo("acme", "widget")
        script = mock_run.call_args.args[0][-1]
        assert "op=fetch" in script
        assert "op=clone" in script

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_ssh_url_gets_https_fallback_in_clone_script(
        self, mock_run: Any, mock_sleep: Any
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="op=clone", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(clone_url_template="git@github.com:{owner}/{repo}.git")
        )
        executor.prepare_repo("acme", "widget")
        script = mock_run.call_args.args[0][-1]
        assert "git@github.com:acme/widget.git" in script
        assert "https://github.com/acme/widget.git" in script

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_https_url_clone_has_no_extra_clone_fallback(
        self, mock_run: Any, mock_sleep: Any
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="op=clone", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(clone_url_template="https://github.com/{owner}/{repo}.git")
        )
        executor.prepare_repo("acme", "widget")
        script = mock_run.call_args.args[0][-1]
        # Clone branch is HTTPS-only (SSH key may not be set up for clone)
        assert script.count("git clone") == 1

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_rc255_logged_as_ssh_connection_error(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=255, stdout="op=fetch", stderr="Connection closed"
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        with caplog.at_level("DEBUG"):
            executor.prepare_repo("acme", "widget")
        assert any("SSH connection error" in r.message for r in caplog.records)

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_nonzero_nonconn_logged_as_remote_command_error(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="op=clone", stderr="repository not found"
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        with caplog.at_level("DEBUG"):
            executor.prepare_repo("acme", "widget")
        assert any("remote command error" in r.message for r in caplog.records)

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_ssh_command_logged_in_debug_on_failure(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="op=fetch", stderr="err"
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        with caplog.at_level("DEBUG", logger="franktheunicorn.review.tool_executor"):
            executor.prepare_repo("acme", "widget")
        # The debug log should contain the ssh command itself
        assert any("cmd:" in r.getMessage() for r in caplog.records)

    # --- progressive verbosity ---

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_first_attempt_uses_quiet_flag(self, mock_run: Any, mock_sleep: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="op=fetch", stderr=""
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        executor.prepare_repo("acme", "widget")
        script = mock_run.call_args_list[0].args[0][-1]
        assert "--quiet" in script
        assert "--verbose" not in script

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_second_attempt_drops_quiet_flag(self, mock_run: Any, mock_sleep: Any) -> None:
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="op=fetch", stderr="err"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="op=fetch", stderr=""),
        ]
        executor = RemoteSSHExecutor(config=_ssh_config())
        executor.prepare_repo("acme", "widget")
        second_script = mock_run.call_args_list[1].args[0][-1]
        assert "--quiet" not in second_script
        assert "--verbose" not in second_script

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_fourth_attempt_uses_verbose_flag(self, mock_run: Any, mock_sleep: Any) -> None:
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="op=fetch", stderr="e"),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="op=fetch", stderr="e"),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="op=fetch", stderr="e"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="op=fetch", stderr=""),
        ]
        executor = RemoteSSHExecutor(config=_ssh_config())
        executor.prepare_repo("acme", "widget")
        fourth_script = mock_run.call_args_list[3].args[0][-1]
        assert "--verbose" in fourth_script
        assert "--quiet" not in fourth_script

    # --- HTTPS fallback in fetch branch ---

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_ssh_url_gets_https_fallback_in_fetch_script(
        self, mock_run: Any, mock_sleep: Any
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="op=fetch", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(clone_url_template="git@github.com:{owner}/{repo}.git")
        )
        executor.prepare_repo("acme", "widget")
        script = mock_run.call_args.args[0][-1]
        # fetch branch should have || fallback to HTTPS URL
        assert "git fetch" in script
        assert "https://github.com/acme/widget.git" in script

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_https_url_gets_ssh_fallback_in_fetch_script(
        self, mock_run: Any, mock_sleep: Any
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="op=fetch", stderr=""
        )
        executor = RemoteSSHExecutor(
            config=_ssh_config(clone_url_template="https://github.com/{owner}/{repo}.git")
        )
        executor.prepare_repo("acme", "widget")
        script = mock_run.call_args.args[0][-1]
        # fetch branch should have || fallback to SSH URL
        assert "git fetch" in script
        assert "git@github.com:acme/widget.git" in script

    # --- all-rc255 log message ---

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_all_rc255_logs_ssh_unreachable_message(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=255, stdout="", stderr="Connection closed"
        )
        executor = RemoteSSHExecutor(config=_ssh_config(port=8032))
        with caplog.at_level("WARNING"):
            executor.prepare_repo("acme", "widget")
        final = [r for r in caplog.records if "unreachable" in r.message]
        assert final, "Expected 'unreachable' in final warning"
        assert "8032" in final[-1].message

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_mixed_errors_do_not_log_ssh_unreachable(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        # One rc=255, rest rc=128 — not all SSH-unreachable
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=128, stdout="op=fetch", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=128, stdout="op=fetch", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=128, stdout="op=fetch", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=128, stdout="op=fetch", stderr=""),
        ]
        executor = RemoteSSHExecutor(config=_ssh_config())
        with caplog.at_level("WARNING"):
            executor.prepare_repo("acme", "widget")
        assert not any("unreachable" in r.message for r in caplog.records)


class TestRemoteSSHExecutorProbeSSH:
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_probe_returns_true_on_success(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(config=_ssh_config())
        assert executor._probe_ssh() is True
        argv = mock_run.call_args.args[0]
        assert "true" in argv
        # Goes through run_script/_run_via_argv now, which does not add its own
        # ConnectTimeout — the 15s subprocess timeout in _probe_ssh is the bound.
        assert "BatchMode=yes" in argv

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_probe_returns_false_on_failure(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=255, stdout="", stderr="Connection refused"
        )
        assert RemoteSSHExecutor(config=_ssh_config())._probe_ssh() is False

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_probe_returns_false_on_timeout(self, mock_run: Any) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=15)
        assert RemoteSSHExecutor(config=_ssh_config())._probe_ssh() is False

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_probe_returns_false_on_missing_binary(self, mock_run: Any) -> None:
        mock_run.side_effect = FileNotFoundError("no ssh")
        assert RemoteSSHExecutor(config=_ssh_config())._probe_ssh() is False

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_probe_fires_warning_on_second_rc255(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        # First two calls are the git attempts (rc=255); third is the SSH probe
        # (also rc=255); remaining calls are further git retries.
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr="conn closed"),
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr="conn closed"),
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr="conn closed"),
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr="conn closed"),
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr="conn closed"),
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr="conn closed"),
        ]
        executor = RemoteSSHExecutor(config=_ssh_config(port=8032))
        with caplog.at_level("WARNING"):
            executor.prepare_repo("acme", "widget")
        transport_warnings = [r for r in caplog.records if "transport" in r.message]
        assert transport_warnings, "Expected SSH transport-down diagnostic warning"
        assert "8032" in transport_warnings[0].message

    @patch("franktheunicorn.review.tool_executor.time.sleep")
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_probe_not_fired_when_probe_succeeds(
        self, mock_run: Any, mock_sleep: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Git attempts return 255; SSH probe succeeds — no transport warning.
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),  # probe ok
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=255, stdout="", stderr=""),
        ]
        executor = RemoteSSHExecutor(config=_ssh_config())
        with caplog.at_level("WARNING"):
            executor.prepare_repo("acme", "widget")
        assert not any("transport" in r.message for r in caplog.records)


class TestGitVerbosityFlag:
    def test_attempt_0_returns_quiet(self) -> None:
        assert _git_verbosity_flag(0) == "--quiet"

    def test_attempt_1_returns_empty(self) -> None:
        assert _git_verbosity_flag(1) == ""

    def test_attempt_2_returns_empty(self) -> None:
        assert _git_verbosity_flag(2) == ""

    def test_attempt_3_returns_verbose(self) -> None:
        assert _git_verbosity_flag(3) == "--verbose"

    def test_attempt_4_returns_verbose(self) -> None:
        assert _git_verbosity_flag(4) == "--verbose"


class TestSshFallbackUrl:
    def test_https_to_ssh(self) -> None:
        assert (
            RemoteSSHExecutor._ssh_fallback_url("https://github.com/owner/repo.git")
            == "git@github.com:owner/repo.git"
        )

    def test_https_without_dotgit(self) -> None:
        assert (
            RemoteSSHExecutor._ssh_fallback_url("https://github.com/owner/repo")
            == "git@github.com:owner/repo.git"
        )

    def test_ssh_url_returns_empty(self) -> None:
        assert RemoteSSHExecutor._ssh_fallback_url("git@github.com:owner/repo.git") == ""

    def test_non_url_returns_empty(self) -> None:
        assert RemoteSSHExecutor._ssh_fallback_url("not-a-url") == ""


class TestExecResult:
    def test_ok_property(self) -> None:
        assert ExecResult(returncode=0, stdout="", stderr="").ok
        assert not ExecResult(returncode=1, stdout="", stderr="").ok


#: Captured before any patching so the fake wrapper below can really run a shell.
_REAL_RUN = subprocess.run


class TestRemoteCommandDelivery:
    """How the command reaches the far side, decided by experiment.

    ``ssh host 'cmd'`` puts it in a trailing argument. Some wrappers use that
    positional slot for something else: ``sf workspace ssh 'cd /x && claude …'``
    answers ``Error: Workspace not found: cd /x && claude …``. Others ignore extra
    arguments and open an interactive shell, which is worse — the session exits on
    EOF with status 0 and no output, and a ``git diff`` that produced no output is
    indistinguishable from a repo with no changes, so the review comes back clean
    and silent.
    """

    @staticmethod
    def _wrapper_config() -> RemoteExecutionConfig:
        return RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])

    @staticmethod
    def _shell(script: str | None) -> subprocess.CompletedProcess[str]:
        """Stand in for a wrapper that only reads commands from stdin.

        Runs the piped script with ``sh`` and prepends banner noise, the way the
        real thing prints its own line and then ssh prints a login banner.
        """
        if script is None:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        # _REAL_RUN, not subprocess.run — the latter is patched in these tests,
        # so calling it here recurses into the fake wrapper forever.
        done = _REAL_RUN(["sh"], input=script, capture_output=True, text=True, check=False)
        banner = "Running: ssh 10.66.76.234\nLast login: Wed Aug 26 17:42:39 2026\n"
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=banner + done.stdout, stderr=done.stderr
        )

    def _fake_sf(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """argv form is rejected as a workspace name; stdin form works."""
        positional = [a for a in argv[3:] if not a.startswith("-")]
        if positional:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=1,
                stdout="",
                stderr=f"Error: Workspace not found: {positional[0]}\n",
            )
        return self._shell(kwargs.get("input"))

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_wrapper_that_rejects_argv_is_driven_over_stdin(self, mock_run: Any) -> None:
        mock_run.side_effect = self._fake_sf
        executor = RemoteSSHExecutor(config=self._wrapper_config())

        result = executor.run(["echo", "hello-from-remote"], cwd="/tmp")

        assert result is not None
        assert result.ok
        assert result.stdout.strip() == "hello-from-remote"

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_the_read_only_flags_reach_the_remote_shell_intact(self, mock_run: Any) -> None:
        """The comma-joined tool list is the argv element a quoting slip would
        mangle. Staged over stdin and run by a real shell, every read-only flag
        must come out as the same argv element."""
        from franktheunicorn.config.models import CLAUDE_READONLY_ARGS, CODEX_READONLY_ARGS

        mock_run.side_effect = self._fake_sf
        executor = RemoteSSHExecutor(config=self._wrapper_config())
        flags = [*CLAUDE_READONLY_ARGS, *CODEX_READONLY_ARGS]

        result = executor.run(["printf", "%s\\n", *flags], cwd="/tmp")

        assert result is not None and result.ok
        assert result.stdout.splitlines()[-len(flags) :] == flags

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_the_wrappers_banner_is_stripped_from_the_output(self, mock_run: Any) -> None:
        """A caller parsing `git diff` must not get "Last login:" prepended."""
        mock_run.side_effect = self._fake_sf
        executor = RemoteSSHExecutor(config=self._wrapper_config())

        result = executor.run(["echo", "diff --git a/x b/x"], cwd="/tmp")

        assert result is not None
        assert "Last login" not in result.stdout
        assert "Running: ssh" not in result.stdout
        assert result.stdout.strip() == "diff --git a/x b/x"

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_the_commands_exit_code_survives_not_the_wrappers(self, mock_run: Any) -> None:
        """The wrapper exits 0 for a session it hosted successfully; the command
        inside it failed, and that is the status callers check."""
        mock_run.side_effect = self._fake_sf
        executor = RemoteSSHExecutor(config=self._wrapper_config())

        result = executor.run(["sh", "-c", "exit 42"], cwd="/tmp")

        assert result is not None
        assert result.returncode == 42
        assert not result.ok

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_the_probe_runs_once_per_config(self, mock_run: Any) -> None:
        """Two extra round trips per command would be unaffordable."""
        mock_run.side_effect = self._fake_sf
        config = self._wrapper_config()

        for _ in range(3):
            RemoteSSHExecutor(config=config).run(["echo", "x"], cwd="/tmp")

        # 2 probes (argv rejected, then stdin accepted) + 3 real commands. The
        # probe is sequential now, so a wrapper whose argv form works pays one.
        assert mock_run.call_count == 5

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_probe_ssh_reports_a_stdin_only_wrapper_as_reachable(self, mock_run: Any) -> None:
        """The generic startup probe used to bypass delivery-mode detection
        entirely — ``subprocess.run([*ssh_command, "true"])`` — which ``sf``
        reads as "run in the workspace named true" and rejects. That reported
        a healthy, working host as down, contradicting the agent-CLI probe's
        own (correct) verdict for the very same target."""
        mock_run.side_effect = self._fake_sf
        executor = RemoteSSHExecutor(config=self._wrapper_config())

        assert executor._probe_ssh() is True

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_plain_ssh_keeps_the_argv_form_without_probing(self, mock_run: Any) -> None:
        """`ssh host 'cmd'` is OpenSSH's documented interface, so confirming it by
        round trip costs a call per config and warns on a healthy host."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(config=_ssh_config())

        executor.run(["true"], cwd="/tmp")

        assert mock_run.call_count == 1, "no probing for real ssh"
        argv = mock_run.call_args.args[0]
        assert argv[-1] == "cd /tmp && true"
        assert mock_run.call_args.kwargs.get("input") is None

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_plain_ssh_never_warns_about_framing(
        self, mock_run: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning on a correct config trains the operator to ignore warnings."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with caplog.at_level("WARNING"):
            RemoteSSHExecutor(config=_ssh_config()).run(["true"], cwd="/tmp")

        assert caplog.text == ""

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_an_explicit_command_mode_skips_the_probe(self, mock_run: Any) -> None:
        mock_run.side_effect = self._fake_sf
        config = RemoteExecutionConfig(
            mode="ssh", ssh_command=["sf", "workspace", "ssh"], command_mode="stdin"
        )

        result = RemoteSSHExecutor(config=config).run(["echo", "hi"], cwd="/tmp")

        assert result is not None
        assert result.stdout.strip() == "hi"
        assert mock_run.call_count == 1, "no probing when the operator already said"

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_neither_shape_working_is_reported(
        self, mock_run: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        executor = RemoteSSHExecutor(config=self._wrapper_config())

        with caplog.at_level("WARNING"):
            executor.run(["true"], cwd="/tmp")

        assert "Neither delivery shape ran a command" in caplog.text

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_failed_probe_is_not_cached(self, mock_run: Any) -> None:
        """The host may just be down; a later cycle should retry rather than be
        stuck on a guess made while it was unreachable."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=255, stdout="", stderr="connection refused"
        )
        config = self._wrapper_config()

        RemoteSSHExecutor(config=config).run(["true"], cwd="/tmp")

        assert config._resolved_command_mode is None

    def test_an_unknown_command_mode_is_rejected_at_load(self) -> None:
        with pytest.raises(ValueError, match="command_mode"):
            RemoteExecutionConfig(mode="ssh", host="h", command_mode="telepathy")


class TestDeliveryHardening:
    """The three ways the first version of this got it wrong."""

    @staticmethod
    def _wrapper() -> RemoteExecutionConfig:
        return RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_wrapper_quoting_our_argv_back_is_not_a_success(self, mock_run: Any) -> None:
        """`Error: Workspace not found: echo <sentinel>` contains the sentinel.

        Caught two ways now: a nonzero exit is checked, and the sentinel is sent
        in halves the remote shell has to join, so an echo of our literal argv
        cannot reproduce it.
        """
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=f"Error: Workspace not found: echo {_DELIVERY_SENTINEL}\n",
            stderr="",
        )
        config = self._wrapper()

        RemoteSSHExecutor(config=config).run(["true"], cwd="/tmp")

        assert config._resolved_command_mode is None, "a rejection must not be cached"

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_an_echoed_sentinel_with_exit_zero_is_still_rejected(self, mock_run: Any) -> None:
        """Belt and braces: even at rc=0, the un-joined form must not pass."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='echo __frank_delivery""_ok__\n',
            stderr="",
        )
        config = self._wrapper()

        RemoteSSHExecutor(config=config).run(["true"], cwd="/tmp")

        assert config._resolved_command_mode is None

    def test_a_pty_echo_returns_the_output_not_the_script(self) -> None:
        """A shell on a PTY echoes the script it is fed, so the markers appear
        twice — taking the first returned the script as the command's output."""
        executor = RemoteSSHExecutor(config=self._wrapper())
        begin, end = "__frank_out_begin__abc", "__frank_out_end__abc"
        echoed = f'echo {begin}\ncd /x && git diff\n__frank_rc=$?\necho "{end}:$__frank_rc"\nexit\n'
        session = ExecResult(
            returncode=0, stdout=f"{echoed}{begin}\nREAL DIFF\n{end}:0\n", stderr=""
        )

        unframed = executor._unframe(session, "git", begin, end)

        assert unframed.stdout.strip() == "REAL DIFF"
        assert unframed.returncode == 0

    def test_output_containing_the_end_marker_is_not_truncated(self) -> None:
        """A `git diff` touching this very file contains the literal marker."""
        executor = RemoteSSHExecutor(config=self._wrapper())
        begin, end = "__frank_out_begin__abc", "__frank_out_end__abc"
        body = f'+_FRAME_END = "{end}"\n+more diff\n'
        session = ExecResult(returncode=0, stdout=f"{begin}\n{body}{end}:0\n", stderr="")

        unframed = executor._unframe(session, "git", begin, end)

        assert unframed.stdout == body
        assert unframed.returncode == 0

    def test_the_markers_are_unique_per_invocation(self) -> None:
        """Which is what makes the collision above survivable at all."""
        seen = set()
        with patch("franktheunicorn.review.tool_executor.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            config = RemoteExecutionConfig(
                mode="ssh", ssh_command=["sf", "workspace", "ssh"], command_mode="stdin"
            )
            for _ in range(3):
                RemoteSSHExecutor(config=config).run(["true"], cwd="/tmp")
                seen.add(mock_run.call_args.kwargs["input"].split("\n", 1)[0])

        assert len(seen) == 3

    def test_a_banner_after_the_exit_code_still_parses(self) -> None:
        """`END:0 Connection to host closed.` used to fail int() and report 1."""
        executor = RemoteSSHExecutor(config=self._wrapper())
        begin, end = "__frank_out_begin__abc", "__frank_out_end__abc"
        session = ExecResult(
            returncode=0,
            stdout=f"{begin}\nok\n{end}:0 Connection to host closed.\n",
            stderr="",
        )

        unframed = executor._unframe(session, "true", begin, end)

        assert unframed.returncode == 0

    def test_an_unparseable_exit_code_is_a_failure_not_a_success(self) -> None:
        executor = RemoteSSHExecutor(config=self._wrapper())
        begin, end = "__frank_out_begin__abc", "__frank_out_end__abc"
        session = ExecResult(returncode=0, stdout=f"{begin}\nok\n{end}: ???\n", stderr="")

        assert executor._unframe(session, "true", begin, end).returncode == 1

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_prepare_repo_honours_the_delivery_mode(self, mock_run: Any) -> None:
        """prepare_repo is the FIRST remote call the review path makes. Going
        straight to argv meant a stdin-only wrapper ignored the clone script,
        exited 0 with no output, and we returned a path never created."""
        config = RemoteExecutionConfig(
            mode="ssh", ssh_command=["sf", "workspace", "ssh"], command_mode="stdin"
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        RemoteSSHExecutor(config=config).prepare_repo("apache", "spark", clone_url="u")

        assert mock_run.call_args.kwargs.get("input") is not None, "clone went over stdin"
        assert "git clone" in mock_run.call_args.kwargs["input"]

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_prepare_repo_does_not_claim_a_clone_that_never_ran(self, mock_run: Any) -> None:
        """No framing back means no proof the script ran, so no success."""
        config = RemoteExecutionConfig(
            mode="ssh", ssh_command=["sf", "workspace", "ssh"], command_mode="stdin"
        )
        # A wrapper that opened a shell and discarded the script: exit 0, no output.
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        assert RemoteSSHExecutor(config=config).prepare_repo("a", "b", clone_url="u") is None

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_dropped_stdin_payload_is_reported(
        self, mock_run: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """LocalExecutor honours stdin and the protocol declares it, so the two
        implementations must not disagree in silence."""
        config = RemoteExecutionConfig(
            mode="ssh", ssh_command=["sf", "workspace", "ssh"], command_mode="stdin"
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with caplog.at_level("WARNING"):
            RemoteSSHExecutor(config=config).run(["cat"], cwd="/tmp", stdin="payload")

        assert "Ignoring a stdin payload" in caplog.text


class TestStdinFramingUnderAPty:
    """The stdin path against a real PTY-attached shell, which is what the
    wrappers this exists for actually open.

    Markers alone got the exit code right and the output wrong: a PTY echoes every
    line fed to it and prints a prompt before each, so the span between the markers
    was a session transcript. The caller that hurt was
    ``claude_code_backend._parse_output``, whose json.loads falls back to returning
    stdout verbatim — so the "model response" was a shell transcript, ``is_error``
    went unchecked and token accounting recorded zero.
    """

    @staticmethod
    def _pty_wrapper(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """A wrapper that opens an interactive shell on a PTY, like the real ones.

        ``os.openpty`` + ``Popen``, not ``pty.spawn``: the latter calls
        ``os.forkpty``, and under the full suite something has already started a
        thread, so Python warns that forking a multi-threaded process may deadlock
        in the child. This shape never forks the interpreter — the child is
        exec'd straight away — and gives the same real terminal.

        ``subprocess.run`` is patched in these tests; ``Popen`` is not, which is
        what makes calling it here safe.
        """
        script = kwargs.get("input")
        if script is None:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        master, slave = os.openpty()
        chunks: list[bytes] = []
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-i"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            # Closed in the parent so the read below sees EOF when the shell exits.
            os.close(slave)
            # Written incrementally while reading, not all at once. A pty's input
            # buffer is a few KB, so a single blocking write of a large script
            # deadlocks: the buffer fills, we never get to the read, and the shell
            # never gets the rest. subprocess.run(input=...) pumps both directions
            # for the same reason, which is why production doesn't hit this.
            #
            # Deadline rather than read-until-EOF, too: a shell that never exits —
            # exactly what an over-long line causes — otherwise hangs the whole
            # suite with no indication which test did it.
            pending = script.encode()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                writable = [master] if pending else []
                readable, ready, _ = select.select([master], writable, [], 0.2)
                if ready:
                    try:
                        written = os.write(master, pending[:2048])
                    except OSError:
                        # EIO: the shell exited while input was still pending, which
                        # is exactly what the over-long-line cases provoke. Guarded
                        # like the read below, because unguarded it escaped into the
                        # mocked subprocess.run, got swallowed by _spawn's own
                        # `except OSError`, and the test then failed as
                        # "assert result is not None" instead of naming the pump.
                        pending = b""
                        continue
                    pending = pending[written:]
                if not readable:
                    if proc.poll() is not None and not pending:
                        break
                    continue
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break  # EIO — the far end of the pty went away
                if not data:
                    break
                chunks.append(data)
            timed_out = time.monotonic() >= deadline
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            # Asserted, not silently fallen out of. The deadline exists so a hung
            # shell names itself; without this it still surfaced as an unrelated
            # assertion further down.
            assert not timed_out, "pty session did not finish within 30s"
        finally:
            os.close(master)

        banner = "Running: ssh 10.66.76.234\r\nLast login: Wed Aug 26 17:42:39 2026\r\n"
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=banner + b"".join(chunks).decode(errors="replace"),
            stderr="",
        )

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_stdout_is_the_commands_output_not_the_session(self, mock_run: Any) -> None:
        mock_run.side_effect = self._pty_wrapper
        executor = RemoteSSHExecutor(
            config=RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        )

        result = executor.run_script("echo REAL_ONE; echo REAL_TWO", timeout=30, label="probe")

        assert result is not None
        assert result.returncode == 0
        assert result.stdout == "REAL_ONE\nREAL_TWO\n"
        # The things that used to come back inside stdout.
        assert "sh-5.1$" not in result.stdout
        assert "\x1b[" not in result.stdout
        assert "__frank_rc" not in result.stdout
        assert "Last login" not in result.stdout

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_json_output_survives_the_round_trip(self, mock_run: Any) -> None:
        """The concrete regression: claude_code_backend json.loads()es this."""
        mock_run.side_effect = self._pty_wrapper
        executor = RemoteSSHExecutor(
            config=RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        )
        payload = '{"result": "looks fine to me", "is_error": false}'

        result = executor.run_script(f"printf '%s' '{payload}'", timeout=30, label="probe")

        assert result is not None
        assert json.loads(result.stdout) == {"result": "looks fine to me", "is_error": False}

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_stderr_is_separated_from_stdout(self, mock_run: Any) -> None:
        """Both were interleaved on the one PTY before; nothing could tell them apart."""
        mock_run.side_effect = self._pty_wrapper
        executor = RemoteSSHExecutor(
            config=RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        )

        result = executor.run_script(
            "echo to-stdout; echo to-stderr 1>&2", timeout=30, label="probe"
        )

        assert result is not None
        assert result.stdout == "to-stdout\n"
        assert "to-stderr" in result.stderr
        assert "to-stderr" not in result.stdout

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_the_real_exit_code_still_comes_back(self, mock_run: Any) -> None:
        mock_run.side_effect = self._pty_wrapper
        executor = RemoteSSHExecutor(
            config=RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        )

        result = executor.run_script("echo nope 1>&2; exit 3", timeout=30, label="probe")

        assert result is not None
        assert result.returncode == 3

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_command_that_prints_the_frame_markers_is_not_confused(self, mock_run: Any) -> None:
        """A git diff touching this very file contains the marker literals."""
        mock_run.side_effect = self._pty_wrapper
        executor = RemoteSSHExecutor(
            config=RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        )

        result = executor.run_script(
            "echo __frank_out_end__deadbeef:99; echo after", timeout=30, label="probe"
        )

        assert result is not None
        assert result.returncode == 0
        assert result.stdout == "__frank_out_end__deadbeef:99\nafter\n"

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_binary_output_does_not_crash_the_executor(self, mock_run: Any) -> None:
        """A diff of a binary file. Strict decoding raised UnicodeDecodeError out of
        subprocess and took the caller with it."""
        mock_run.side_effect = self._pty_wrapper
        executor = RemoteSSHExecutor(
            config=RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        )

        result = executor.run_script(r"printf 'a\377\376b'", timeout=30, label="probe")

        assert result is not None
        assert result.returncode == 0
        assert "a" in result.stdout and "b" in result.stdout


class TestSpawnFailureModes:
    """What the OS can refuse, and whether it reaches the caller as a diagnosis."""

    @staticmethod
    def _config(command: list[str]) -> RemoteExecutionConfig:
        return RemoteExecutionConfig(mode="ssh", ssh_command=command)

    def test_a_wrapper_missing_chmod_x_is_a_warning_not_a_traceback(
        self, tmp_path: Any, caplog: Any
    ) -> None:
        wrapper = tmp_path / "sf-wrapper"
        wrapper.write_text("#!/bin/sh\necho hi\n")
        wrapper.chmod(0o644)
        executor = RemoteSSHExecutor(config=self._config([str(wrapper)]))

        with caplog.at_level(logging.WARNING):
            result = executor.run(["echo", "hi"], cwd="/tmp", timeout=5)

        assert result is None
        assert "not executable" in caplog.text

    def test_an_ssh_command_pointing_at_a_directory_is_handled(
        self, tmp_path: Any, caplog: Any
    ) -> None:
        executor = RemoteSSHExecutor(config=self._config([str(tmp_path)]))

        with caplog.at_level(logging.WARNING):
            result = executor.run(["echo", "hi"], cwd="/tmp", timeout=5)

        assert result is None
        assert "not executable" in caplog.text or "Could not run" in caplog.text


class TestLongCommandsOverAPty:
    """A pty in canonical mode will not take a line at or over MAX_CANON (4096 on
    Linux), and the failure is not truncation — measured here, a 4,090-byte line is
    discarded entirely and the shell hangs waiting for a newline it never gets,
    while ~14,000 bytes runs the command on a corrupted fragment.

    Not a corner case: the verifier's prompt is ~13.8 KB at the default
    max_report_chars and an agent-CLI review carries up to max_diff_chars=60,000,
    so every long remote command over a stdin-only wrapper was landing in there.
    """

    @staticmethod
    def _wrapper(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return TestStdinFramingUnderAPty._pty_wrapper(argv, **kwargs)

    def _executor(self) -> RemoteSSHExecutor:
        return RemoteSSHExecutor(
            config=RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        )

    @pytest.mark.parametrize("size", [4_090, 8_000, 14_000, 60_000])
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_long_command_still_runs_and_returns_its_output(
        self, mock_run: Any, size: int
    ) -> None:
        mock_run.side_effect = self._wrapper
        # A payload of exactly `size` bytes inside the command, echoed back out.
        payload = "P" * size
        result = self._executor().run_script(
            f"printf '%s' '{payload}' | wc -c | tr -d ' '", timeout=60, label="long"
        )

        assert result is not None, f"{size}-byte command produced no result at all"
        assert result.returncode == 0
        assert result.stdout.strip() == str(size)

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_long_command_is_staged_rather_than_typed(self, mock_run: Any) -> None:
        """The executed line has to be short regardless of how long the command is."""
        mock_run.side_effect = self._wrapper
        self._executor().run_script("echo " + "z" * 9_000, timeout=60, label="long")

        script = mock_run.call_args.kwargs["input"]
        longest = max(len(line) for line in script.splitlines())
        assert longest < 2100, f"a {longest}-byte line would be eaten by the pty"
        assert "FRANK_EOF_" in script  # staged via a quoted heredoc

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_short_command_is_not_staged(self, mock_run: Any) -> None:
        """Staging costs two temp files and a decode; most calls here are
        `git rev-parse`-sized."""
        mock_run.side_effect = self._wrapper
        self._executor().run_script("echo hi", timeout=60, label="short")

        assert "FRANK_EOF_" not in mock_run.call_args.kwargs["input"]

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_command_that_reads_stdin_does_not_eat_the_framing(self, mock_run: Any) -> None:
        """``codex exec`` reads stdin to EOF. The framing script is that stdin, so
        without a redirect the agent consumes the lines that retrieve its output
        and the session ends with only the begin marker."""
        mock_run.side_effect = self._wrapper

        result = self._executor().run_script("cat", timeout=30, label="codex")

        assert result is not None
        assert result.returncode == 0
        assert result.stdout == ""
        assert "< /dev/null" in mock_run.call_args.kwargs["input"]

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_staged_command_keeps_its_quoting_and_newlines(self, mock_run: Any) -> None:
        """base64 inside a quoted heredoc, so a payload full of single quotes,
        newlines and shell metacharacters survives verbatim."""
        mock_run.side_effect = self._wrapper
        nasty = 'it\'s "quoted" $HOME `backtick` \\ end'
        result = self._executor().run_script(
            f"printf '%s' {shlex.quote(nasty)}; echo; echo {'x' * 5000} > /dev/null",
            timeout=60,
            label="nasty",
        )

        assert result is not None
        assert result.stdout.splitlines()[0] == nasty


class TestMissingBase64OnTheRemote:
    """Without base64 the framing still printed both payload markers around an
    empty span, so "the encoder is missing" and "the command printed nothing" were
    the same observation — and downstream, a git diff with no output is
    indistinguishable from a clean repo."""

    @staticmethod
    def _wrapper_without_base64(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        script = kwargs.get("input")
        if script is None:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        # Shadow base64 with a failing stub, the way a stripped-down remote behaves.
        shimmed = "base64() { return 127; }\nunalias base64 2>/dev/null\n" + script
        return TestStdinFramingUnderAPty._pty_wrapper(argv, input=shimmed)

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_it_refuses_rather_than_reporting_empty_output(
        self, mock_run: Any, caplog: Any
    ) -> None:
        """None, not rc=0 with empty stdout. The command's output goes to a file on
        the remote and only base64 retrieves it, so with no encoder the output
        exists and is unreachable — and "no output" would read downstream as a
        clean repo or an empty diff."""
        mock_run.side_effect = self._wrapper_without_base64
        executor = RemoteSSHExecutor(
            config=RemoteExecutionConfig(
                mode="ssh", ssh_command=["sf", "workspace", "ssh"], command_mode="stdin"
            )
        )

        with caplog.at_level(logging.WARNING):
            result = executor.run_script("echo IMPORTANT_DIFF_LINE", timeout=60, label="probe")

        assert result is None
        # Names the whole retrieval pipeline, not just base64: `head -c` or `tr`
        # missing produces the identical observation, and telling the operator to
        # install coreutils when `head` is the problem is the opposite of a
        # diagnostic.
        assert "head -c | base64 | tr -d" in caplog.text
        assert "command_mode" in caplog.text


class TestLocalIsolatedCheckout:
    """`workspace_subdir` was accepted and ignored in local mode, so the security
    verifier — which asks for isolation precisely because it runs
    `git checkout --detach --force` onto arbitrary release branches — was handed
    the review pipeline's own clone. Every run left it detached on the last branch
    looked at, and commands drain mid-cycle, so the rest of that poll cycle read
    blame and copy-pasta context from the wrong branch."""

    @staticmethod
    def _repo(tmp_path: Path) -> Path:
        repo = tmp_path / "repos" / "apache" / "spark"
        repo.mkdir(parents=True)
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "HOME": str(tmp_path),
        }
        for argv in (
            ["git", "init", "-q", "-b", "master"],
            ["git", "commit", "-q", "--allow-empty", "-m", "first"],
        ):
            subprocess.run(
                argv, cwd=repo, check=True, capture_output=True, env={**os.environ, **env}
            )
        return repo

    def test_a_subdir_request_gets_a_tree_of_its_own(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)

        cwd = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )

        assert cwd is not None
        assert cwd != str(repo)
        assert "security-verify" in cwd
        assert (Path(cwd) / ".git").exists()

    def test_detaching_the_isolated_tree_leaves_the_shared_one_alone(self, tmp_path: Path) -> None:
        """The actual harm: the verifier checks out a release branch and the review
        pipeline then reads source from it."""
        repo = self._repo(tmp_path)
        env = {**os.environ, "HOME": str(tmp_path)}
        subprocess.run(
            ["git", "branch", "branch-3.5"], cwd=repo, check=True, capture_output=True, env=env
        )
        cwd = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )
        assert cwd is not None

        subprocess.run(
            ["git", "checkout", "--detach", "--force", "branch-3.5"],
            cwd=cwd,
            check=True,
            capture_output=True,
            env=env,
        )

        shared_head = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )
        assert shared_head.returncode == 0
        assert shared_head.stdout.strip() == "master"

    def test_no_subdir_still_returns_the_shared_clone(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        assert LocalExecutor().prepare_repo("apache", "spark", local_path=repo) == str(repo)

    def test_a_second_call_reuses_the_worktree(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        first = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )
        second = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )
        assert first == second

    def test_it_refuses_rather_than_handing_back_the_shared_clone(
        self, tmp_path: Path, caplog: Any
    ) -> None:
        """If isolation can't be had, the caller must not silently get the tree it
        was trying to avoid touching."""
        not_a_repo = tmp_path / "repos" / "apache" / "spark"
        not_a_repo.mkdir(parents=True)

        with caplog.at_level(logging.WARNING):
            cwd = LocalExecutor().prepare_repo(
                "apache", "spark", local_path=not_a_repo, workspace_subdir="security-verify"
            )

        assert cwd is None
        assert "isolated worktree" in caplog.text


class TestWorktreeSelfHealing:
    """The `.git`-exists fast path was wrong in two ways that both end in a
    permanently dead tree, and the docstring claimed it self-healed."""

    @staticmethod
    def _repo(tmp_path: Path) -> Path:
        repo = tmp_path / "repos" / "apache" / "spark"
        repo.mkdir(parents=True)
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        }
        for argv in (
            ["git", "init", "-q", "-b", "master"],
            ["git", "commit", "-q", "--allow-empty", "-m", "first"],
        ):
            subprocess.run(argv, cwd=repo, check=True, capture_output=True, env=env)
        return repo

    def test_the_isolation_root_matches_the_remote_layout(self, tmp_path: Path) -> None:
        """`local_path.parent` nested it inside the owner namespace —
        `<repos>/apache/security-verify/apache/spark` — which diverges from the
        remote's `<workspace>/security-verify/<owner>/<repo>` and collides with a
        project literally named `apache/security-verify`."""
        repo = self._repo(tmp_path)

        cwd = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )

        assert cwd is not None
        assert Path(cwd) == tmp_path / "repos" / "security-verify" / "apache" / "spark"

    def test_a_pruned_worktree_is_rebuilt_not_handed_back_dead(self, tmp_path: Path) -> None:
        """git gc --auto runs `worktree prune`. The .git file survives, so the old
        fast path returned a tree where every command answered
        `fatal: not a git repository: (null)` — forever."""
        repo = self._repo(tmp_path)
        env = {**os.environ, "HOME": str(tmp_path)}
        cwd = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )
        assert cwd is not None
        # Simulate the prune: drop the admin dir, leave the .git file behind.
        shutil.rmtree(repo / ".git" / "worktrees")
        assert (Path(cwd) / ".git").exists()

        again = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )

        assert again is not None
        probe = subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=again, capture_output=True, text=True, env=env
        )
        assert probe.returncode == 0, f"handed back a dead worktree: {probe.stderr}"

    def test_a_relocated_worktree_is_rebuilt(self, tmp_path: Path) -> None:
        """A worktree's .git records an ABSOLUTE gitdir, and this project runs the
        same data/ tree at two prefixes (docker compose /app vs host)."""
        repo = self._repo(tmp_path)
        env = {**os.environ, "HOME": str(tmp_path)}
        cwd = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )
        assert cwd is not None
        # Point the .git file at a gitdir that doesn't exist, as a moved prefix does.
        (Path(cwd) / ".git").write_text("gitdir: /nonexistent/elsewhere/.git/worktrees/spark\n")

        again = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )

        assert again is not None
        probe = subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=again, capture_output=True, text=True, env=env
        )
        assert probe.returncode == 0, f"handed back a dead worktree: {probe.stderr}"

    def test_a_healthy_worktree_is_reused_not_rebuilt(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        first = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )
        assert first is not None
        marker = Path(first) / "marker.txt"
        marker.write_text("still here")

        second = LocalExecutor().prepare_repo(
            "apache", "spark", local_path=repo, workspace_subdir="security-verify"
        )

        assert second == first
        assert marker.read_text() == "still here", "a healthy worktree was rebuilt"


class TestTruncatedOutputIsAnnounced:
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_hitting_the_output_cap_is_logged(self, mock_run: Any, caplog: Any) -> None:
        """The remote declares its untruncated size precisely so this is detectable,
        and the comparison was written into a docstring and never into the code — so
        a 4 MB-plus payload came back silently short and the verifier's verdict JSON
        was cut off the end."""
        mock_run.side_effect = TestStdinFramingUnderAPty._pty_wrapper
        executor = RemoteSSHExecutor(
            config=RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        )

        with (
            patch("franktheunicorn.review.tool_executor._MAX_REMOTE_OUTPUT_BYTES", 100),
            caplog.at_level(logging.WARNING),
        ):
            result = executor.run_script("printf '%5000s' x", timeout=60, label="big")

        assert result is not None
        assert len(result.stdout) == 100
        assert "truncated" in caplog.text
        assert "5000" in caplog.text


class TestDeliveryProbeWithoutBase64:
    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_base64_less_remote_still_resolves_to_stdin(self, mock_run: Any) -> None:
        """The refusal for un-retrievable output also killed the capability probe, so
        such a remote resolved to argv — a mode the wrapper cannot use — and left the
        result uncached, re-probing twice per command forever."""
        mock_run.side_effect = TestMissingBase64OnTheRemote._wrapper_without_base64
        config = RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        executor = RemoteSSHExecutor(config=config)

        mode = executor._delivery_mode(timeout=60)

        assert mode == "stdin"
        assert config._resolved_command_mode == "stdin", "the mode was not cached"


class TestWorkspaceLane:
    """Concurrent users of the checkouts must not share a tree."""

    def teardown_method(self) -> None:
        from franktheunicorn.review.tool_executor import set_workspace_lane

        set_workspace_lane("")

    def test_no_lane_leaves_the_subdir_alone(self) -> None:
        from franktheunicorn.review.tool_executor import lane_subdir

        assert lane_subdir("") == ""
        assert lane_subdir("security-verify") == "security-verify"

    def test_a_lane_isolates_even_when_the_caller_wanted_no_subdir(self) -> None:
        """The review pipeline reads the main clone directly, and that is exactly
        the tree a concurrent force-run must not detach HEAD in."""
        from franktheunicorn.review.tool_executor import lane_subdir, set_workspace_lane

        set_workspace_lane("force-run")
        assert lane_subdir("") == "force-run"
        assert lane_subdir("security-verify") == "security-verify-force-run"

    def test_the_lane_does_not_leak_between_threads(self) -> None:
        import threading

        from franktheunicorn.review.tool_executor import lane_subdir, set_workspace_lane

        seen: list[str] = []

        def _worker() -> None:
            set_workspace_lane("force-run")
            seen.append(lane_subdir("security-verify"))

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join()

        assert seen == ["security-verify-force-run"]
        # The poll cycle's own thread is untouched.
        assert lane_subdir("security-verify") == "security-verify"

    def test_local_executor_puts_a_lane_in_its_own_worktree(self, tmp_path: Any) -> None:
        import subprocess

        from franktheunicorn.review.tool_executor import LocalExecutor, set_workspace_lane

        repo = tmp_path / "repos" / "apache" / "spark"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
            cwd=repo,
            check=True,
        )

        executor = LocalExecutor()
        plain = executor.prepare_repo("apache", "spark", local_path=repo)
        set_workspace_lane("force-run")
        laned = executor.prepare_repo("apache", "spark", local_path=repo)

        assert plain == str(repo)
        assert laned is not None
        assert laned != plain
        assert "force-run" in laned


class TestRemoteLaneUsesAWorktree:
    """A clone per lane is gigabytes on the remote for a tree that only needs a
    different HEAD, so a lane is a linked worktree of the un-laned clone."""

    def teardown_method(self) -> None:
        from franktheunicorn.review.tool_executor import set_workspace_lane

        set_workspace_lane("")

    @staticmethod
    def _ok() -> Any:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="op=fetch\n", stderr="")

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_lane_adds_a_worktree_off_the_base_clone(self, mock_run: Any) -> None:
        from franktheunicorn.review.tool_executor import set_workspace_lane

        mock_run.return_value = self._ok()
        set_workspace_lane("force-run")
        executor = RemoteSSHExecutor(config=_ssh_config())
        cwd = executor.prepare_repo("acme", "widget")

        assert cwd == "/srv/frank/force-run/acme/widget"
        scripts = "\n".join(str(call.args[0][-1]) for call in mock_run.call_args_list)
        assert "worktree add --detach --force" in scripts
        assert "/srv/frank/force-run/acme/widget" in scripts
        # One clone/fetch, for the base tree — the lane does not clone again.
        assert scripts.count("git clone") == 1
        assert "/srv/frank/acme/widget" in scripts

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_failed_worktree_returns_none_not_the_shared_tree(self, mock_run: Any) -> None:
        """Sharing it puts a force-run's `git checkout --detach` back into the tree
        the poll cycle is reading a diff out of — the bug the lane prevents."""
        from franktheunicorn.review.tool_executor import set_workspace_lane

        def _run(argv: Any, **_kwargs: Any) -> Any:
            script = str(argv[-1])
            code = 1 if "worktree add" in script else 0
            return subprocess.CompletedProcess(
                args=[], returncode=code, stdout="", stderr="fatal: nope" if code else ""
            )

        mock_run.side_effect = _run
        set_workspace_lane("force-run")
        executor = RemoteSSHExecutor(config=_ssh_config())
        assert executor.prepare_repo("acme", "widget") is None

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_no_lane_is_the_plain_clone_path(self, mock_run: Any) -> None:
        mock_run.return_value = self._ok()
        executor = RemoteSSHExecutor(config=_ssh_config())
        cwd = executor.prepare_repo("acme", "widget")

        assert cwd == "/srv/frank/acme/widget"
        scripts = "\n".join(str(call.args[0][-1]) for call in mock_run.call_args_list)
        assert "worktree add" not in scripts

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_a_lane_widens_an_existing_workspace_subdir(self, mock_run: Any) -> None:
        """The verifier already has its own tree; a force-run of it needs a third."""
        from franktheunicorn.review.tool_executor import set_workspace_lane

        mock_run.return_value = self._ok()
        set_workspace_lane("force-run")
        executor = RemoteSSHExecutor(config=_ssh_config())
        cwd = executor.prepare_repo("acme", "widget", workspace_subdir="security-verify")

        assert cwd == "/srv/frank/security-verify-force-run/acme/widget"
        scripts = "\n".join(str(call.args[0][-1]) for call in mock_run.call_args_list)
        # Hung off the verifier's own clone, not the review pipeline's.
        assert "/srv/frank/security-verify/acme/widget" in scripts


# ---------------------------------------------------------------------------
# The checkout-config guard around agent runs
# ---------------------------------------------------------------------------


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
    "GIT_CONFIG_GLOBAL": os.devnull,
}

#: dash is what ``sh`` is on Debian and Ubuntu; bash in POSIX mode is what it is
#: on macOS, the other likely remote.
_SHELLS = [pytest.param(["sh"], id="sh")] + (
    [pytest.param(["bash", "--posix"], id="bash-posix")] if shutil.which("bash") else []
)


def _git(repo: Path, *args: str) -> str:
    done = _REAL_RUN(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def _pr_checkout(tmp_path: Path) -> Path:
    """A checkout carrying every kind of entry the guard has to move: tracked
    config the PR wrote, a tracked file the PR edited, and an untracked file the
    operator keeps there."""
    repo = tmp_path / "checkout"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".pi" / "extensions").mkdir(parents=True)
    (repo / ".cursor").mkdir()
    (repo / "src").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}\n')
    (repo / ".pi" / "extensions" / "x.ts").write_text("export default () => {}\n")
    (repo / ".cursor" / "hooks.json").write_text("{}\n")
    (repo / ".mcp.json").write_text("{}\n")
    (repo / "src" / "a.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pr")
    (repo / ".claude" / "settings.local.json").write_text("operator's own\n")
    (repo / ".mcp.json").write_text('{"edited": true}\n')
    return repo


def _snapshot(repo: Path) -> dict[str, str]:
    return {
        str(p.relative_to(repo)): p.read_text()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }


def _with_shell(argv: list[str], shell: list[str]) -> list[str]:
    assert argv[0] == "sh"
    return [*shell, *argv[1:]]


_LIST_ENTRIES = ["sh", "-c", "ls -A; exit 3"]


class TestAgentConfigGuard:
    """A PR can ship config that an agent CLI executes on startup: claude and
    cursor-agent hooks, pi extensions and package installs, MCP servers. The
    guard has to keep all of it out of the agent's sight and then hand the tree
    back exactly as it was, because some of it is the operator's own."""

    @pytest.mark.parametrize("shell", _SHELLS)
    def test_the_agent_sees_none_of_it_and_the_tree_comes_back(
        self, tmp_path: Path, shell: list[str]
    ) -> None:
        from franktheunicorn.review.tool_executor import guard_agent_argv

        repo = _pr_checkout(tmp_path)
        before, status = _snapshot(repo), _git(repo, "status", "--porcelain")

        done = _REAL_RUN(
            _with_shell(guard_agent_argv(_LIST_ENTRIES), shell),
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert done.stdout.split() == [".git", "src"]
        assert done.returncode == 3
        assert _snapshot(repo) == before
        assert _git(repo, "status", "--porcelain") == status

    def test_every_listed_entry_is_moved(self, tmp_path: Path) -> None:
        from franktheunicorn.review.tool_executor import AGENT_CONFIG_ENTRIES, guard_agent_argv

        repo = tmp_path / "checkout"
        repo.mkdir()
        _git(repo, "init", "-q")
        for entry in AGENT_CONFIG_ENTRIES:
            (repo / entry).write_text("x\n")

        done = _REAL_RUN(guard_agent_argv(_LIST_ENTRIES), cwd=repo, capture_output=True, text=True)

        assert done.stdout.split() == [".git"]
        assert sorted(os.listdir(repo)) == sorted([".git", *AGENT_CONFIG_ENTRIES])

    def test_a_symlinked_entry_is_moved_as_a_link(self, tmp_path: Path) -> None:
        """``.claude -> ~/.claude`` must hide the link, not empty the operator's
        own directory through it."""
        from franktheunicorn.review.tool_executor import guard_agent_argv

        home_claude = tmp_path / "home" / ".claude"
        home_claude.mkdir(parents=True)
        (home_claude / "settings.json").write_text("operator\n")
        repo = tmp_path / "checkout"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / ".claude").symlink_to(home_claude)

        done = _REAL_RUN(
            guard_agent_argv(["sh", "-c", 'ls -A; ls -A "$0"', str(home_claude)]),
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert done.stdout.split() == [".git", "settings.json"]
        assert (repo / ".claude").is_symlink()
        assert (home_claude / "settings.json").read_text() == "operator\n"

    def test_a_missing_agent_binary_moves_nothing(self, tmp_path: Path) -> None:
        from franktheunicorn.review.tool_executor import guard_agent_argv

        repo = _pr_checkout(tmp_path)
        before = _snapshot(repo)

        done = _REAL_RUN(
            guard_agent_argv(["frank-no-such-agent", "-p", "x"]),
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert done.returncode == 127
        assert "frank-no-such-agent: command not found" in done.stderr
        assert _snapshot(repo) == before

    def test_outside_a_git_checkout_it_refuses(self, tmp_path: Path) -> None:
        from franktheunicorn.review.tool_executor import GUARD_REFUSED_EXIT, guard_agent_argv

        plain = tmp_path / "plain"
        (plain / ".claude").mkdir(parents=True)

        done = _REAL_RUN(
            guard_agent_argv(["sh", "-c", "echo AGENT RAN"]),
            cwd=plain,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
        )

        assert done.returncode == GUARD_REFUSED_EXIT
        assert "AGENT RAN" not in done.stdout
        assert "not running the agent" in done.stderr

    def test_an_entry_that_will_not_move_refuses_and_restores_the_rest(
        self, tmp_path: Path
    ) -> None:
        """The agent must not start with one entry in place, and the entries
        already moved must not be stranded by the refusal."""
        from franktheunicorn.review.tool_executor import GUARD_REFUSED_EXIT, guard_agent_argv

        repo = _pr_checkout(tmp_path)
        before = _snapshot(repo)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        real_mv = shutil.which("mv")
        (bin_dir / "mv").write_text(
            f'#!/bin/sh\ncase "$2" in ./.mcp.json) exit 1;; esac\nexec {real_mv} "$@"\n'
        )
        (bin_dir / "mv").chmod(0o755)

        done = _REAL_RUN(
            guard_agent_argv(["sh", "-c", "echo AGENT RAN"]),
            cwd=repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        )

        assert done.returncode == GUARD_REFUSED_EXIT
        assert "AGENT RAN" not in done.stdout
        assert "could not move .mcp.json out" in done.stderr
        assert _snapshot(repo) == before

    def test_a_timeout_still_puts_everything_back(self, tmp_path: Path) -> None:
        """``_run_local`` sends TERM and waits before its SIGKILL; the guard's
        trap stops the agent and restores in that window."""
        from franktheunicorn.review.tool_executor import guard_agent_argv

        repo = _pr_checkout(tmp_path)
        before = _snapshot(repo)

        result = LocalExecutor().run(guard_agent_argv(["sleep", "30"]), cwd=str(repo), timeout=1)

        assert result is None
        assert _snapshot(repo) == before

    def test_a_run_killed_outright_is_put_back_by_the_next(self, tmp_path: Path) -> None:
        import signal

        from franktheunicorn.review.tool_executor import guard_agent_argv

        repo = _pr_checkout(tmp_path)
        before = _snapshot(repo)
        proc = subprocess.Popen(guard_agent_argv(["sleep", "30"]), cwd=repo, start_new_session=True)
        deadline = time.monotonic() + 10
        while (repo / ".claude").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        assert not (repo / ".claude").exists()

        done = _REAL_RUN(guard_agent_argv(_LIST_ENTRIES), cwd=repo, capture_output=True, text=True)

        assert done.stdout.split() == [".git", "src"]
        assert _snapshot(repo) == before

    def test_a_killed_run_does_not_block_checking_out_the_next_pr(self, tmp_path: Path) -> None:
        """The PR under review edited a tracked config file and the run was killed
        with it stashed. The next PR's non-forced checkout must still work (git
        reads a missing tracked file as up to date), and the next guarded run
        must not resurrect the killed PR's version over the new one."""
        import signal

        from franktheunicorn.review.tool_executor import GUARD_STASH, guard_agent_argv

        repo = _pr_checkout(tmp_path)
        (repo / ".mcp.json").write_text("{}\n")
        (repo / ".claude" / "settings.local.json").unlink()
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / ".claude" / "settings.json").write_text('{"hooks": {"x": 1}}\n')
        _git(repo, "commit", "-qam", "the PR edits its hooks")
        proc = subprocess.Popen(guard_agent_argv(["sleep", "30"]), cwd=repo, start_new_session=True)
        deadline = time.monotonic() + 10
        while (repo / ".claude").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()

        _git(repo, "checkout", "-q", "--detach", base)
        done = _REAL_RUN(guard_agent_argv(_LIST_ENTRIES), cwd=repo, capture_output=True, text=True)

        assert done.stdout.split() == [".git", "src"]
        assert (repo / ".claude" / "settings.json").read_text() == '{"hooks": {}}\n'
        assert _git(repo, "status", "--porcelain") == ""
        assert not any((repo / ".git" / GUARD_STASH).iterdir())

    def test_a_stashed_copy_the_tree_has_again_is_kept_not_deleted(self, tmp_path: Path) -> None:
        """A forced checkout put a fresh tracked ``.claude`` back after a killed
        run; the stashed one held the operator's untracked file too."""
        from franktheunicorn.review.tool_executor import GUARD_STASH, guard_agent_argv

        repo = _pr_checkout(tmp_path)
        git_dir = repo / ".git"
        stash = git_dir / GUARD_STASH
        stash.mkdir()
        (repo / ".claude").rename(stash / ".claude")
        _git(repo, "checkout", "-q", "--", ".claude")

        done = _REAL_RUN(guard_agent_argv(_LIST_ENTRIES), cwd=repo, capture_output=True, text=True)

        assert done.returncode == 3
        assert "from an interrupted agent run is in" in done.stderr
        kept = list((git_dir / f"{GUARD_STASH}.displaced").glob("*/.claude/settings.local.json"))
        assert [p.read_text() for p in kept] == ["operator's own\n"]
        assert not (stash / ".claude").exists()

    def test_a_live_run_holding_the_checkout_refuses_a_second(self, tmp_path: Path) -> None:
        """Two runs in one tree would have the second's recovery hand the
        first's agent its hooks back."""
        from franktheunicorn.review.tool_executor import (
            GUARD_REFUSED_EXIT,
            GUARD_STASH,
            guard_agent_argv,
        )

        repo = _pr_checkout(tmp_path)
        before = _snapshot(repo)
        lock = repo / ".git" / f"{GUARD_STASH}.lock"
        lock.mkdir()
        (lock / "pid").write_text(f"{os.getpid()}\n")

        done = _REAL_RUN(
            guard_agent_argv(["sh", "-c", "echo AGENT RAN"]),
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert done.returncode == GUARD_REFUSED_EXIT
        assert "AGENT RAN" not in done.stdout
        assert f"agent run {os.getpid()} holds" in done.stderr
        assert _snapshot(repo) == before
        assert lock.exists()  # still the other run's

    def test_a_dead_runs_lock_is_taken_over_and_released(self, tmp_path: Path) -> None:
        from franktheunicorn.review.tool_executor import GUARD_STASH, guard_agent_argv

        repo = _pr_checkout(tmp_path)
        gone = subprocess.Popen(["true"])
        gone.wait()
        lock = repo / ".git" / f"{GUARD_STASH}.lock"
        lock.mkdir()
        (lock / "pid").write_text(f"{gone.pid}\n")

        done = _REAL_RUN(guard_agent_argv(_LIST_ENTRIES), cwd=repo, capture_output=True, text=True)

        assert done.stdout.split() == [".git", "src"]
        assert not lock.exists()

    def test_the_agent_gets_no_stdin(self, tmp_path: Path) -> None:
        """Remotely the shell's stdin is the delivery stream; an agent reading
        it would eat the rest of the script."""
        from franktheunicorn.review.tool_executor import guard_agent_argv

        repo = _pr_checkout(tmp_path)
        done = _REAL_RUN(
            guard_agent_argv(["sh", "-c", "cat; echo end"]),
            cwd=repo,
            input="delivery stream\n",
            capture_output=True,
            text=True,
        )

        assert done.stdout == "end\n"

    def test_unwrap_guarded(self) -> None:
        from franktheunicorn.review.tool_executor import guard_agent_argv, unwrap_guarded

        argv = ["claude", "-p", "review this"]
        assert unwrap_guarded(guard_agent_argv(argv)) == argv
        assert unwrap_guarded(argv) is argv
        assert unwrap_guarded(["sh", "-c", "true"]) == ["sh", "-c", "true"]

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_it_survives_stdin_delivery_to_a_remote(self, mock_run: Any, tmp_path: Path) -> None:
        from franktheunicorn.review.tool_executor import guard_agent_argv

        repo = _pr_checkout(tmp_path)
        before = _snapshot(repo)
        delivery = TestRemoteCommandDelivery()
        mock_run.side_effect = delivery._fake_sf
        executor = RemoteSSHExecutor(config=delivery._wrapper_config())

        result = executor.run(guard_agent_argv(_LIST_ENTRIES), cwd=str(repo))

        assert result is not None
        assert result.returncode == 3
        assert result.stdout.split()[-2:] == [".git", "src"]
        assert _snapshot(repo) == before

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_it_survives_argv_delivery_to_a_remote(self, mock_run: Any, tmp_path: Path) -> None:
        """ssh joins its trailing arguments into one string for the remote
        shell; the guard script's quotes and newlines must survive that."""
        from franktheunicorn.review.tool_executor import guard_agent_argv

        repo = _pr_checkout(tmp_path)
        before = _snapshot(repo)

        def _ssh(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return _REAL_RUN(["sh", "-c", " ".join(argv[1:])], capture_output=True, text=True)

        mock_run.side_effect = _ssh
        executor = RemoteSSHExecutor(config=RemoteExecutionConfig(mode="ssh", ssh_command=["s"]))

        result = executor.run(guard_agent_argv(_LIST_ENTRIES), cwd=str(repo))

        assert result is not None
        assert result.returncode == 3
        assert result.stdout.split() == [".git", "src"]
        assert _snapshot(repo) == before


class TestLocalChildrenAtShutdown:
    """Local agents run in their own session, so a Ctrl-C or hangup at the
    worker's terminal no longer reaches them; the worker has to stop them."""

    def test_stop_local_children_ends_a_run_on_another_thread(self, tmp_path: Path) -> None:
        import threading

        from franktheunicorn.review.tool_executor import guard_agent_argv, stop_local_children

        repo = _pr_checkout(tmp_path)
        before = _snapshot(repo)
        results: list[Any] = []
        worker = threading.Thread(
            target=lambda: results.append(
                LocalExecutor().run(guard_agent_argv(["sleep", "60"]), cwd=str(repo), timeout=120)
            ),
            daemon=True,
        )
        started = time.monotonic()
        worker.start()
        deadline = time.monotonic() + 10
        while (repo / ".claude").exists() and time.monotonic() < deadline:
            time.sleep(0.05)

        assert stop_local_children() == 1
        worker.join(timeout=15)

        assert not worker.is_alive()
        assert time.monotonic() - started < 30
        assert results and results[0] is not None and results[0].returncode == 143
        assert _snapshot(repo) == before  # TERM first, so the guard put it back
        assert stop_local_children() == 0

    def test_ctrl_c_in_the_running_thread_terms_before_it_kills(self, tmp_path: Path) -> None:
        import _thread
        import threading

        from franktheunicorn.review.tool_executor import _run_local, guard_agent_argv

        repo = _pr_checkout(tmp_path)
        before = _snapshot(repo)

        def _interrupt_once_moved() -> None:
            deadline = time.monotonic() + 10
            while (repo / ".claude").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            _thread.interrupt_main()

        threading.Thread(target=_interrupt_once_moved, daemon=True).start()
        with pytest.raises(KeyboardInterrupt):
            _run_local(guard_agent_argv(["sleep", "60"]), str(repo), 120, None)

        assert _snapshot(repo) == before
