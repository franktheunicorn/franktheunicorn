"""Tests for the persistent hardened tool sandbox (worker/tool_sandbox.py)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from franktheunicorn.worker.tool_sandbox import (
    ToolSandbox,
    _hardened_run_kwargs,
    tool_sandbox_session,
)


def _fake_container(exec_return=(0, (b"out", b""))) -> MagicMock:
    container = MagicMock()
    container.exec_run.return_value = exec_return
    return container


class TestHardenedRunKwargs:
    def test_security_flags_present(self, tmp_path: Path) -> None:
        kwargs = _hardened_run_kwargs(
            "python:3.12-slim",
            tmp_path,
            {"cpu_count": 2, "mem_limit": "4g"},
            120,
        )
        assert kwargs["network_mode"] == "none"
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["read_only"] is True
        assert kwargs["security_opt"] == ["no-new-privileges"]
        assert kwargs["pids_limit"] == 256
        assert kwargs["working_dir"] == "/workspace"
        # Repo bound read-only.
        bind = kwargs["volumes"][str(tmp_path)]
        assert bind == {"bind": "/workspace", "mode": "ro"}
        # Keep-alive command self-terminates at the budget.
        assert kwargs["command"] == ["sh", "-c", "sleep 120"]
        assert "/frank-scratch" in kwargs["tmpfs"]


class TestToolSandboxExec:
    def test_exec_passes_argv_list_as_nobody(self) -> None:
        container = _fake_container()
        sandbox = ToolSandbox(container, total_budget_seconds=60, per_call_timeout=10)
        result = sandbox.exec(["rg", "--", "needle", "."])
        assert result.exit_code == 0
        assert result.stdout == "out"
        assert result.timed_out is False
        # argv passed as a list; runs as nobody with demux.
        args, kwargs = container.exec_run.call_args
        assert args[0] == ["rg", "--", "needle", "."]
        assert kwargs["user"] == "65534:65534"
        assert kwargs["demux"] is True

    def test_exec_decodes_and_truncates_output(self) -> None:
        big = b"x" * 1000
        container = _fake_container(exec_return=(0, (big, b"err")))
        sandbox = ToolSandbox(
            container, total_budget_seconds=60, per_call_timeout=10, max_output_bytes=100
        )
        result = sandbox.exec(["echo"])
        assert result.stdout.startswith("x" * 100)
        assert "truncated" in result.stdout
        assert result.stderr == "err"

    def test_exec_handles_none_output(self) -> None:
        container = _fake_container(exec_return=(0, None))
        sandbox = ToolSandbox(container, total_budget_seconds=60, per_call_timeout=10)
        result = sandbox.exec(["true"])
        assert result.stdout == ""
        assert result.stderr == ""

    def test_per_call_timeout_returns_timed_out(self) -> None:
        container = MagicMock()

        def slow(*_a, **_k):
            time.sleep(1.5)
            return (0, (b"", b""))

        container.exec_run.side_effect = slow
        sandbox = ToolSandbox(container, total_budget_seconds=60, per_call_timeout=1)
        result = sandbox.exec(["sleep"])
        assert result.timed_out is True
        assert "timed out" in result.stderr.lower()

    def test_total_budget_short_circuit(self) -> None:
        container = _fake_container()
        sandbox = ToolSandbox(container, total_budget_seconds=10, per_call_timeout=5)
        # Simulate the budget already being spent.
        sandbox._elapsed = 11.0
        result = sandbox.exec(["rg"])
        assert result.timed_out is True
        assert "budget" in result.stderr.lower()
        container.exec_run.assert_not_called()

    def test_exec_error_returns_error_result(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = RuntimeError("boom")
        sandbox = ToolSandbox(container, total_budget_seconds=60, per_call_timeout=10)
        result = sandbox.exec(["rg"])
        assert result.exit_code == -1
        assert result.timed_out is False
        assert "boom" in result.stderr


class TestToolAvailable:
    def test_available_true_and_cached(self) -> None:
        container = _fake_container(exec_return=(0, (b"/usr/bin/rg", b"")))
        sandbox = ToolSandbox(container, total_budget_seconds=60, per_call_timeout=10)
        assert sandbox.tool_available("rg") is True
        assert sandbox.tool_available("rg") is True
        # Probed only once (cached).
        assert container.exec_run.call_count == 1

    def test_unavailable_false(self) -> None:
        container = _fake_container(exec_return=(1, (b"", b"not found")))
        sandbox = ToolSandbox(container, total_budget_seconds=60, per_call_timeout=10)
        assert sandbox.tool_available("ctags") is False


class TestToolSandboxSession:
    def test_yields_sandbox_and_removes_container(self, tmp_path: Path) -> None:
        container = _fake_container()
        docker = MagicMock()
        docker.containers.run.return_value = container

        with tool_sandbox_session(
            docker,
            "python:3.12-slim",
            tmp_path,
            resources={"cpu_count": 2, "mem_limit": "4g"},
            total_budget_seconds=30,
            per_call_timeout=5,
        ) as sandbox:
            assert isinstance(sandbox, ToolSandbox)

        docker.containers.run.assert_called_once()
        container.remove.assert_called_once_with(force=True)

    def test_container_removed_on_exception(self, tmp_path: Path) -> None:
        container = _fake_container()
        docker = MagicMock()
        docker.containers.run.return_value = container

        with (
            pytest.raises(ValueError, match="inside"),
            tool_sandbox_session(
                docker,
                "img",
                tmp_path,
                resources={},
                total_budget_seconds=30,
                per_call_timeout=5,
            ),
        ):
            raise ValueError("inside")

        container.remove.assert_called_once_with(force=True)

    def test_remove_failure_is_swallowed(self, tmp_path: Path) -> None:
        container = _fake_container()
        container.remove.side_effect = RuntimeError("already gone")
        docker = MagicMock()
        docker.containers.run.return_value = container

        # Must not raise even though cleanup fails.
        with tool_sandbox_session(
            docker,
            "img",
            tmp_path,
            resources={},
            total_budget_seconds=30,
            per_call_timeout=5,
        ):
            pass
