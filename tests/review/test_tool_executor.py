"""Tests for the local + remote-SSH tool executor."""

from __future__ import annotations

import json
import logging
import os
import select
import shlex
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
                    written = os.write(master, pending[:2048])
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
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
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
        assert "base64(1)" in caplog.text
        # And it names both ways out.
        assert "coreutils" in caplog.text
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
