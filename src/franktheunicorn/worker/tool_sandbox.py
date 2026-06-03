"""Persistent, hardened container for agentic review tools.

The agentic review loop (``review/agent_tools.py`` driven by the Claude
backend) issues many short, read-only commands — grep, find, read_file,
list_symbols — during a single review. Rather than pay container create +
teardown latency on every call, this module starts **one** hardened container
per review and runs each tool via ``docker exec``.

Security posture mirrors the differential test runner
(``worker/test_runner.py`` ``TestRunner._run_container`` is the canonical
source for these flags) and adopts the stricter additions from
``security/sandbox.py``: no network, all capabilities dropped, read-only root,
no new privileges, a pids limit, tmpfs scratch, the repo bound read-only, and
exec'd processes running as the unprivileged ``nobody`` user.

Only the worker imports this module — it must never be imported from the web
container, which has no Docker socket.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Unprivileged user for exec'd tool processes (matches security/sandbox.py).
_NOBODY = "65534:65534"
_SCRATCH = "/frank-scratch"
_WORKSPACE = "/workspace"


@dataclass
class ToolCommandResult:
    """Result of running one tool command inside the sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


def _hardened_run_kwargs(
    image: str,
    workspace: Path,
    resources: dict[str, Any],
    total_budget_seconds: int,
) -> dict[str, Any]:
    """Build the ``containers.run`` kwargs for a hardened tool container.

    Canonical source for these flags is ``TestRunner._run_container``; we add
    ``pids_limit`` (from ``security/sandbox.py``) and a keep-alive command so
    the container stays up for the review and self-terminates afterwards.
    """
    return {
        "image": image,
        # Keep-alive: an orphaned container (worker crash) self-terminates once
        # the review's total time budget elapses.
        "command": ["sh", "-c", f"sleep {total_budget_seconds}"],
        "detach": True,
        "network_mode": "none",
        "cpu_count": resources.get("cpu_count", 2),
        "mem_limit": resources.get("mem_limit", "4g"),
        "security_opt": ["no-new-privileges"],
        "cap_drop": ["ALL"],
        "read_only": True,
        "pids_limit": 256,
        "tmpfs": {"/tmp": "size=256m", _SCRATCH: "size=512m,exec"},
        "volumes": {str(workspace): {"bind": _WORKSPACE, "mode": "ro"}},
        "working_dir": _WORKSPACE,
        "environment": {"HOME": _SCRATCH, "TMPDIR": _SCRATCH},
    }


class ToolSandbox:
    """A running container that executes many short read-only tool commands.

    Commands are passed as **argv lists** (never shell strings) so untrusted
    model input can never become a shell token. A per-call timeout and a
    cumulative wall-clock budget bound execution; once the total budget is
    exhausted, further calls short-circuit to a ``timed_out`` result so the
    agentic loop terminates gracefully instead of hanging.
    """

    def __init__(
        self,
        container: Any,
        *,
        total_budget_seconds: int,
        per_call_timeout: int,
        max_output_bytes: int = 64_000,
    ) -> None:
        self._container = container
        self._total_budget = total_budget_seconds
        self._per_call_timeout = per_call_timeout
        self._max_output_bytes = max_output_bytes
        self._elapsed = 0.0
        self._available: dict[str, bool] = {}

    def _budget_remaining(self) -> float:
        return self._total_budget - self._elapsed

    def _truncate(self, text: str) -> str:
        if len(text) <= self._max_output_bytes:
            return text
        return text[: self._max_output_bytes] + "\n... (output truncated)"

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str = _WORKSPACE,
        timeout: int | None = None,
    ) -> ToolCommandResult:
        """Run ``argv`` inside the container as the ``nobody`` user.

        ``docker exec`` (via the SDK ``exec_run``) has no native timeout, so we
        run it in a worker thread and abandon it on timeout. On timeout, or once
        the cumulative budget is spent, returns ``timed_out=True``.
        """
        if self._budget_remaining() <= 0:
            return ToolCommandResult(
                exit_code=-1,
                stdout="",
                stderr="Tool time budget exhausted for this review.",
                timed_out=True,
            )

        per_call = timeout if timeout is not None else self._per_call_timeout
        # Never wait longer than the remaining overall budget.
        effective = max(1, min(per_call, int(self._budget_remaining())))

        start = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    self._container.exec_run,
                    argv,
                    workdir=cwd,
                    user=_NOBODY,
                    demux=True,
                )
                try:
                    exit_code, output = future.result(timeout=effective)
                except FutureTimeout:
                    return ToolCommandResult(
                        exit_code=-1,
                        stdout="",
                        stderr=f"Tool call timed out after {effective}s.",
                        timed_out=True,
                    )
        except Exception as exc:
            logger.debug("exec_run failed for %r", argv, exc_info=True)
            return ToolCommandResult(
                exit_code=-1,
                stdout="",
                stderr=f"Tool execution error: {exc}",
                timed_out=False,
            )
        finally:
            self._elapsed += time.monotonic() - start

        stdout_b, stderr_b = output if output is not None else (None, None)
        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        return ToolCommandResult(
            exit_code=exit_code if exit_code is not None else -1,
            stdout=self._truncate(stdout),
            stderr=self._truncate(stderr),
            timed_out=False,
        )

    def tool_available(self, binary: str) -> bool:
        """Return True if ``binary`` is on PATH inside the container (cached)."""
        if binary in self._available:
            return self._available[binary]
        result = self.exec(["command", "-v", binary])
        available = result.exit_code == 0 and not result.timed_out
        self._available[binary] = available
        return available


@contextmanager
def tool_sandbox_session(
    docker: Any,
    image: str,
    workspace: Path,
    *,
    resources: dict[str, Any],
    total_budget_seconds: int,
    per_call_timeout: int,
    max_output_bytes: int = 64_000,
) -> Iterator[ToolSandbox]:
    """Start one hardened container bound to ``workspace`` and yield a sandbox.

    The container is always removed (``remove(force=True)``) on exit, even if
    the body raises.
    """
    container = docker.containers.run(
        **_hardened_run_kwargs(image, workspace, resources, total_budget_seconds)
    )
    try:
        yield ToolSandbox(
            container,
            total_budget_seconds=total_budget_seconds,
            per_call_timeout=per_call_timeout,
            max_output_bytes=max_output_bytes,
        )
    finally:
        try:
            container.remove(force=True)
        except Exception:
            logger.warning("Failed to remove tool sandbox container", exc_info=True)


__all__ = ["ToolCommandResult", "ToolSandbox", "tool_sandbox_session"]
