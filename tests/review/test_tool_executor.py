"""Tests for the local + remote-SSH tool executor."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import RemoteExecutionConfig
from franktheunicorn.review.tool_executor import (
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
        assert "-o" in argv and "BatchMode=yes" in argv
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
        # BatchMode etc. comes after the wrapper prefix.
        assert argv[3:5] == ["-o", "BatchMode=yes"]

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
        # _ssh_command must not append an empty string AND must not add SSH-specific
        # option flags that the wrapper doesn't understand (-o BatchMode=yes etc.).
        cfg = RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        executor = RemoteSSHExecutor(config=cfg)
        argv = executor._ssh_command()
        assert "" not in argv
        # Wrapper mode: only the base command, no -o flags, no host
        assert argv == ["sf", "workspace", "ssh"]

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_wrapper_command_probe_uses_dash_c_true(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        cfg = RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        executor = RemoteSSHExecutor(config=cfg)
        executor._probe_ssh()
        argv = mock_run.call_args.args[0]
        assert argv == ["sf", "workspace", "ssh", "-c", "true"]
        # Must NOT include SSH-specific flags
        assert "-o" not in argv
        assert "BatchMode=yes" not in argv
        assert "ConnectTimeout=10" not in argv

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_wrapper_command_run_uses_dash_c(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        cfg = RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        executor = RemoteSSHExecutor(config=cfg)
        executor.run(["true"], cwd="/home/frank/repo")
        argv = mock_run.call_args.args[0]
        # Wrapper: sf workspace ssh -c "cd /path && true"
        assert argv[0:3] == ["sf", "workspace", "ssh"]
        assert argv[3] == "-c"
        assert "true" in argv[4]
        assert "-o" not in argv

    @patch("franktheunicorn.review.tool_executor.subprocess.run")
    def test_wrapper_command_prepare_repo_uses_dash_c(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="op=clone", stderr=""
        )
        cfg = RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        executor = RemoteSSHExecutor(config=cfg)
        executor.prepare_repo("owner", "repo")
        argv = mock_run.call_args.args[0]
        assert argv[0:3] == ["sf", "workspace", "ssh"]
        assert argv[3] == "-c"
        assert "git clone" in argv[4] or "git fetch" in argv[4]


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
        assert "ConnectTimeout=10" in argv

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
