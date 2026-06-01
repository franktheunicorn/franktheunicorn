"""Worker-side dispatcher for ``WorkerCommand`` rows queued by the dashboard.

The dashboard never spawns containers itself. Instead, the operator's click
turns into a ``WorkerCommand`` row with ``status="pending"`` and the worker
process picks it up here and runs the heavy work (Docker, LLM calls, git
operations) inside its own container where Docker access is permitted.

Commands supported:
- ``run_dual_tests``: differential test verification on a PR.
- ``run_security_sandbox``: execute a security-report POC in the sandbox.
- ``run_agents``: force-run the review pipeline on a PR (no trusted-author
  gate, no dedup against existing drafts).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from franktheunicorn.core.models import WorkerCommand

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig

logger = logging.getLogger(__name__)


def process_pending_commands(operator_config: OperatorConfig) -> int:
    """Pick up and execute every pending WorkerCommand.

    Returns the number of commands processed (success or failure).
    Each command is claimed atomically by flipping ``pending → running``
    inside a transaction so two workers can't double-run the same row.
    """
    processed = 0
    pending_ids = list(
        WorkerCommand.objects.filter(status="pending")
        .order_by("created_at")
        .values_list("pk", flat=True)
    )
    for cmd_id in pending_ids:
        cmd = _claim_command(cmd_id)
        if cmd is None:
            continue
        try:
            _dispatch(cmd, operator_config)
            cmd.status = "completed"
        except Exception as exc:
            logger.exception("WorkerCommand #%d (%s) failed", cmd.pk, cmd.command)
            cmd.status = "failed"
            cmd.error = f"{type(exc).__name__}: {exc}"[:5000]
        finally:
            cmd.finished_at = timezone.now()
            cmd.save(update_fields=["status", "error", "log", "finished_at"])
            processed += 1
    return processed


def _claim_command(cmd_id: int) -> WorkerCommand | None:
    """Atomically transition a command from pending → running.

    Returns the locked row, or ``None`` if another worker already grabbed it.
    """
    with transaction.atomic():
        try:
            cmd = WorkerCommand.objects.select_for_update().get(pk=cmd_id)
        except WorkerCommand.DoesNotExist:
            return None
        if cmd.status != "pending":
            return None
        cmd.status = "running"
        cmd.started_at = timezone.now()
        cmd.save(update_fields=["status", "started_at"])
        return cmd


def _dispatch(cmd: WorkerCommand, operator_config: OperatorConfig) -> None:
    """Route a claimed command to its handler. Mutates ``cmd.log`` on success."""
    handlers = {
        "run_dual_tests": _run_dual_tests,
        "run_security_sandbox": _run_security_sandbox,
        "run_agents": _run_agents,
    }
    handler = handlers.get(cmd.command)
    if handler is None:
        msg = f"Unknown WorkerCommand command={cmd.command!r}"
        raise ValueError(msg)
    handler(cmd, operator_config)


def _resolve_repo_path(owner: str, repo: str) -> Path | None:
    """Return the local checkout path for a project, if it exists."""
    from django.conf import settings

    repos_dir = getattr(settings, "FRANK_REPOS_DIR", "")
    if not repos_dir:
        return None
    candidate = Path(repos_dir) / owner / repo
    return candidate if candidate.is_dir() else None


def _run_dual_tests(cmd: WorkerCommand, operator_config: OperatorConfig) -> None:
    if cmd.pull_request is None:
        msg = "run_dual_tests requires a pull_request target"
        raise ValueError(msg)

    from franktheunicorn.config.loader import get_project_config
    from franktheunicorn.worker.test_runner import TestRunner

    pr = cmd.pull_request
    project_config = get_project_config(pr.project.full_name)
    if project_config is None:
        msg = f"No project config for {pr.project.full_name}"
        raise ValueError(msg)
    if not project_config.tests.enabled:
        msg = "Differential tests are not enabled for this project"
        raise ValueError(msg)

    repo_path = _resolve_repo_path(pr.project.owner, pr.project.repo)

    runner = TestRunner()
    test_run = runner.run_differential_test(pr, project_config, repo_path=repo_path, force=True)
    cmd.log = (
        f"TestRun id={test_run.pk} verdict={test_run.differential_verdict or '<pending>'}"
        if test_run is not None
        else "Test run produced no result"
    )


def _run_security_sandbox(cmd: WorkerCommand, operator_config: OperatorConfig) -> None:
    if cmd.security_report is None:
        msg = "run_security_sandbox requires a security_report target"
        raise ValueError(msg)

    from franktheunicorn.security.sandbox import run_poc_in_sandbox

    report = cmd.security_report
    repo_path: Path | None = None
    if report.project_id is not None:
        repo_path = _resolve_repo_path(report.project.owner, report.project.repo)

    result = run_poc_in_sandbox(report, repo_path=repo_path)
    report.sandbox_requested = True
    report.sandbox_verdict = result.verdict
    report.sandbox_result = result.output
    report.save(
        update_fields=[
            "sandbox_requested",
            "sandbox_verdict",
            "sandbox_result",
            "updated_at",
        ]
    )
    cmd.log = f"Sandbox verdict={result.verdict}"


def _run_agents(cmd: WorkerCommand, operator_config: OperatorConfig) -> None:
    if cmd.pull_request is None:
        msg = "run_agents requires a pull_request target"
        raise ValueError(msg)

    from franktheunicorn.config.loader import get_project_config
    from franktheunicorn.worker.runner import process_pr

    pr = cmd.pull_request
    project_config = get_project_config(pr.project.full_name)
    if project_config is None:
        msg = f"No project config for {pr.project.full_name}"
        raise ValueError(msg)

    log_lines: list[str] = []
    drafts = process_pr(pr, project_config, operator_config, force=True, log_lines=log_lines)
    summary = f"Generated {len(drafts)} finding(s)"
    if log_lines:
        joined = "\n".join(log_lines[-50:])  # cap log size
        cmd.log = f"{summary}\n{joined}"
    else:
        cmd.log = summary
