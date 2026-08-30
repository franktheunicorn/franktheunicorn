"""Worker-side dispatcher for ``WorkerCommand`` rows queued by the dashboard.

The dashboard never spawns containers itself. Instead, the operator's click
turns into a ``WorkerCommand`` row with ``status="pending"`` and the worker
process picks it up here and runs the heavy work (Docker, LLM calls, git
operations) inside its own container where Docker access is permitted.

Commands supported:
- ``run_dual_tests``: differential test verification on a PR.
- ``run_security_sandbox``: execute a security-report POC in the sandbox.
- ``run_security_triage``: LLM triage of a security report (NVD + two LLM calls).
- ``verify_security_report``: put a coding agent in a checkout and have it read
  the code, once per active branch, to say whether the reported vulnerability is
  actually there. The long one — see ``STUCK_COMMAND_TIMEOUT_SECONDS``.
- ``map_report_versions``: cheap version mapping — git ls-tree of cited files
  on every active release branch, no agent. Minutes for a backlog, not hours.
- ``find_report_introduction``: date the vulnerable code with a git pickaxe walk
  and list the release tags containing that commit. Git only, no agent.
- ``run_agents``: force-run the review pipeline on a PR (no trusted-author
  gate, no dedup against existing drafts).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from franktheunicorn.core.models import WorkerCommand

if TYPE_CHECKING:
    from franktheunicorn.backends.base import ForgeClient
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig

logger = logging.getLogger(__name__)


def _forge_client_for(
    project_config: ProjectConfig, operator_config: OperatorConfig
) -> ForgeClient | None:
    """Best-effort ForgeClient for a project's configured forge.

    Returns None (and process_pr falls back to the changed-files placeholder)
    when no matching forge is registered or the type isn't supported — the
    dashboard trigger must never hard-fail on a diff-fetch setup problem.
    """
    try:
        from franktheunicorn.backends import make_client
        from franktheunicorn.config.resolver import get_forge_entry

        return make_client(get_forge_entry(operator_config, project_config.forge))
    except Exception:
        logger.debug(
            "Could not build forge client for %s; diff falls back to placeholder",
            project_config.full_name,
            exc_info=True,
        )
        return None


#: How many commands one drain will run before handing control back.
#:
#: The drain is called from inside the poll cycle and from the worker's sleep
#: loop, and it used to run the whole backlog: one bulk import with --triage meant
#: a single drain call sat there for a thousand NVD lookups and two thousand LLM
#: calls, during which the poll cycle made no progress and a SIGTERM waited.
#: Bounded, the backlog still drains — across successive drains five seconds
#: apart — and everything else keeps its turn.
MAX_COMMANDS_PER_DRAIN = 20


def process_pending_commands(
    operator_config: OperatorConfig,
    *,
    limit: int | None = None,
    min_priority: int | None = None,
) -> int:
    """Run pending WorkerCommands, best first, up to *limit* of them.

    Returns the number of commands processed (success or failure). Each command
    is claimed atomically by flipping ``pending → running`` inside a transaction
    so two workers can't double-run the same row.

    *min_priority* skips anything below it. That is what makes the worker's
    interactive thread worth having: it must not pick up a bulk row, because a
    bulk row takes exactly as long as the poll cycle it is trying to jump.

    Re-queries between commands rather than taking one snapshot of the pending
    set up front, and that is the point: a snapshot fixes the running order at
    the moment the drain started, so "Force Run Agents" clicked while a hundred
    imported reports were being triaged went to the *back* of that batch no
    matter what priority it carried. Re-querying means the next command is always
    the best one that exists right now — one extra indexed SELECT per command,
    against work that takes tens of seconds.
    """
    budget = MAX_COMMANDS_PER_DRAIN if limit is None else limit
    processed = 0
    while processed < budget:
        pending = WorkerCommand.objects.filter(status="pending")
        if min_priority is not None:
            pending = pending.filter(priority__gte=min_priority)
        cmd_id = pending.order_by("-priority", "created_at").values_list("pk", flat=True).first()
        if cmd_id is None:
            break
        cmd = _claim_command(cmd_id)
        if cmd is None:
            # Another worker took it between the SELECT and the claim. Counted
            # against the budget so a row we keep losing the race for cannot spin
            # this loop — the queue is shared with a second worker only during a
            # restart overlap, but an unbounded retry there is a hang.
            processed += 1
            continue
        started = timezone.now()
        logger.info(
            "WorkerCommand #%d (%s, priority %d) on %s: starting",
            cmd.pk,
            cmd.command,
            cmd.priority,
            _target_label(cmd),
        )
        try:
            _dispatch(cmd, operator_config)
            cmd.status = "completed"
        except Exception as exc:
            logger.exception("WorkerCommand #%d (%s) failed", cmd.pk, cmd.command)
            cmd.status = "failed"
            cmd.error = f"{type(exc).__name__}: {exc}"[:5000]
        except BaseException:
            # SIGTERM arrives as KeyboardInterrupt (see runner). Without this
            # the finally below would persist status="running" forever.
            cmd.status = "failed"
            cmd.error = "Interrupted by worker shutdown"
            raise
        finally:
            cmd.finished_at = timezone.now()
            cmd.save(update_fields=["status", "error", "log", "finished_at"])
            processed += 1
            # At INFO with the elapsed time, because "the button did nothing" is
            # almost always "it ran and took 90 seconds" or "it ran and found
            # nothing", and neither was visible in the log before.
            logger.info(
                "WorkerCommand #%d (%s) %s in %.1fs: %s",
                cmd.pk,
                cmd.command,
                cmd.status,
                (cmd.finished_at - started).total_seconds(),
                (cmd.error or cmd.log or "(no output)").splitlines()[0][:200],
            )
    return processed


def _target_label(cmd: WorkerCommand) -> str:
    if cmd.pull_request is not None:
        return f"{cmd.pull_request.project} #{cmd.pull_request.number}"
    if cmd.security_report is not None:
        return f"report #{cmd.security_report.pk}"
    return "(no target)"


def requeue_interrupted_commands() -> int:
    """Reset commands stranded in ``running`` by a dead worker back to pending.

    Called once at worker startup, *after* the single-instance flock is held —
    at that point no other worker can legitimately own a running command, so
    anything still marked ``running`` was orphaned by a crash/kill and would
    otherwise stay in-flight forever (the worker is required to be safe to
    kill and restart).
    """
    stale = WorkerCommand.objects.filter(status="running")
    count = stale.update(status="pending", started_at=None)
    if count:
        logger.info("Requeued %d WorkerCommand row(s) orphaned by a previous worker.", count)
    return count


#: How long a command may sit in ``running`` before this worker calls it hung.
#:
#: Sized off the longest legitimate handler, which is ``verify_security_report``:
#: ``verifier.timeout_seconds`` (1800 by default) times ``max_branches`` + 1, plus
#: a clone of something the size of Spark before any of that. Four branches at the
#: defaults is two hours of agent time alone, so a three-hour ceiling would fail
#: runs that were about to succeed — and the cost of being wrong in that direction
#: is losing work, where being wrong in the other direction just means a hung row
#: sits a few hours longer than it had to.
#:
#: If you raise ``verifier.timeout_seconds`` or ``max_branches`` much past the
#: defaults, raise this too.
STUCK_COMMAND_TIMEOUT_SECONDS = 6 * 60 * 60


def fail_stuck_commands(now: datetime | None = None) -> int:
    """Fail commands still ``running`` long past any plausible handler.

    The gap ``requeue_interrupted_commands`` leaves. That one reasons — correctly
    — that a ``running`` row at *startup* was orphaned by a crash, and it is the
    only thing that clears one. So a handler that hangs rather than crashing, in a
    worker that stays up, leaves its row ``running`` forever: the in-flight
    constraint then dedups every later click on that target, the operator is told
    "Reused in-flight" each time, and nothing will ever finish it. ``started_at``
    was already being recorded and nothing read it.

    Failed rather than requeued, deliberately. Requeueing would hand the same
    command straight back to the same handler that just hung on it and dedup would
    hide the loop; a failed row clears the constraint, shows the operator an error
    they can act on, and leaves re-running it their decision.
    """
    reference = now or timezone.now()
    cutoff = reference - timedelta(seconds=STUCK_COMMAND_TIMEOUT_SECONDS)
    stuck = WorkerCommand.objects.filter(
        status="running", started_at__isnull=False, started_at__lt=cutoff
    )
    # Named before the update, because afterwards the queryset is empty and the
    # log line is the only record of which target was abandoned.
    doomed = [(cmd.pk, cmd.command, _target_label(cmd)) for cmd in stuck]
    if not doomed:
        return 0
    hours = STUCK_COMMAND_TIMEOUT_SECONDS // 3600
    count = stuck.update(
        status="failed",
        error=(
            f"No result after {hours}h — the worker gave up on it. "
            "The handler hung rather than failing; re-run it if you still want it."
        ),
        # `reference`, not a fresh now(): the parameter exists so a caller (a test,
        # a backfill) can pin the clock, and reading it for the cutoff while
        # stamping from the real clock made it half-honoured.
        finished_at=reference,
    )
    for pk, command, target in doomed:
        logger.warning(
            "WorkerCommand #%d (%s for %s) sat in 'running' for over %dh and has been "
            "failed. Until now that row would have deduped every retry of the same "
            "command, silently.",
            pk,
            command,
            target,
            hours,
        )
    return count


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
        "run_security_triage": _run_security_triage,
        "verify_security_report": _verify_security_report,
        "map_report_versions": _map_report_versions,
        "find_report_introduction": _find_report_introduction,
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


def _ensure_fresh_repo(owner: str, repo: str) -> Path | None:
    """Clone-or-fetch the local checkout, falling back to whatever is on disk.

    A fetch failure is not a reason to abandon the run: a stale tree still gives
    better blame and context than none. It says so, at WARNING, because a review
    against a stale tree can report a finding the PR already fixed.
    """
    from django.conf import settings

    repos_dir = getattr(settings, "FRANK_REPOS_DIR", "")
    if not repos_dir:
        return None
    try:
        from franktheunicorn.worker.repo_manager import ensure_repo

        fresh = ensure_repo(Path(repos_dir), owner, repo)
    except Exception:
        logger.warning("Could not refresh the %s/%s clone (see DEBUG)", owner, repo, exc_info=True)
        fresh = None
    if fresh is not None:
        return fresh
    stale = _resolve_repo_path(owner, repo)
    if stale is not None:
        logger.warning(
            "Using the existing %s/%s clone without a fresh fetch; findings may refer to "
            "code the PR has already changed.",
            owner,
            repo,
        )
    return stale


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
    project = report.project
    if project is not None:
        repo_path = _resolve_repo_path(project.owner, project.repo)

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


def _run_security_triage(cmd: WorkerCommand, operator_config: OperatorConfig) -> None:
    if cmd.security_report is None:
        msg = "run_security_triage requires a security_report target"
        raise ValueError(msg)

    from franktheunicorn.config.loader import get_project_config
    from franktheunicorn.security.triage import triage_report

    report = cmd.security_report
    project = report.project
    project_config = get_project_config(project.full_name) if project is not None else None

    triage_report(report, project_config, operator_config)
    report.refresh_from_db()
    cmd.log = f"Triage complete: severity={report.assessed_severity!r} status={report.status!r}"

    # The version follow-on: triage ruled the report valid-looking, so now go
    # find where the hole lives across release branches — the cheap git version
    # map first, then the deep coding-agent verifier. Skipped for invalid and
    # expected-behavior verdicts (no hole to map) and for inconclusive ones
    # (no point spending the agent runs on a maybe). Gated on the verifier
    # config inside queue_version_follow_on, so a deployment with the verifier
    # off queues nothing rather than fanning out no-op commands. The status
    # check is defense-in-depth: triage only stages "valid" when the operator
    # didn't rule mid-run, but a stale auto_triage_status from a prior run could
    # survive an operator "invalid" verdict set through a path that didn't clear
    # it — so don't bill agent runs on a report the operator ruled not-a-vuln.
    if report.auto_triage_status == "valid" and report.status in ("new", "triaging", "valid"):
        from franktheunicorn.security.queue import queue_version_follow_on

        vm, verify, skipped = queue_version_follow_on(report, operator_config)
        if skipped:
            logger.info(
                "Did not queue version follow-on for report #%d after triage: %s",
                report.pk,
                skipped,
            )
        else:
            cmd.log += f" follow-on: version_map={vm} verify={verify}"


def _verify_security_report(cmd: WorkerCommand, operator_config: OperatorConfig) -> None:
    """Send one report to the deep verifier: is the vulnerability actually there?

    Runs here rather than in the request for the same reason the sandbox does —
    it is minutes of agent time per branch, on a checkout it may have to fetch
    first — and the whole outcome goes into ``cmd.log`` so the operator can see
    what happened even when every branch came back "unclear".
    """
    if cmd.security_report is None:
        msg = "verify_security_report requires a security_report target"
        raise ValueError(msg)

    from franktheunicorn.security.verifier import verify_report

    run = verify_report(cmd.security_report, operator_config)
    lines = [run.summary()]
    for result in run.results:
        confidence = f" confidence={result.confidence:.2f}" if result.confidence is not None else ""
        lines.append(f"  {result.branch}: {result.verdict}{confidence} ({result.agent})")
    cmd.log = "\n".join(lines)
    if run.error:
        # A gate declining is not a crash, so this stays a successful command with
        # a log that says why nothing happened — the alternative is a red "failed"
        # for `enabled: false`, which sends the operator hunting for a bug.
        logger.info(
            "Verification for report #%s did not run: %s", cmd.security_report.pk, run.error
        )


def _map_report_versions(cmd: WorkerCommand, operator_config: OperatorConfig) -> None:
    """Map cited files onto every active release branch. Git only, no agent."""
    if cmd.security_report is None:
        msg = "map_report_versions requires a security_report target"
        raise ValueError(msg)

    from franktheunicorn.security.version_map import map_report_versions

    run = map_report_versions(cmd.security_report, operator_config)
    lines = [run.summary()]
    for result in run.results:
        lines.append(f"  {result.branch}: {result.verdict} ({result.agent})")
    cmd.log = "\n".join(lines)
    if run.error:
        logger.info(
            "Version mapping for report #%s did not run: %s", cmd.security_report.pk, run.error
        )


def _find_report_introduction(cmd: WorkerCommand, operator_config: OperatorConfig) -> None:
    """Date the vulnerable code from git history and list the releases with it."""
    if cmd.security_report is None:
        msg = "find_report_introduction requires a security_report target"
        raise ValueError(msg)

    from franktheunicorn.security.introduced import find_introduction, persist_introduction

    run = find_introduction(cmd.security_report, operator_config)
    persist_introduction(cmd.security_report, run)
    lines = [run.summary()]
    for origin in run.origins:
        lines.append(f"  {origin.path}: {origin.error or origin.commit[:12]}")
    cmd.log = "\n".join(lines)
    if run.error:
        logger.info(
            "Introduction scan for report #%s did not run: %s",
            cmd.security_report.pk,
            run.error,
        )


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

    # Pass the local clone like the scheduled path does — without it,
    # blame/repo context and local-mode CLI tools are silently skipped and
    # "Force Run Agents" produces a weaker review than the poll cycle.
    #
    # ensure_repo, not _resolve_repo_path: that one only checks the directory is
    # there, so a force-run reviewed whatever the last poll happened to leave in
    # the tree. The poll path fetches first and this is the path where freshness
    # matters most — the operator clicked it because the PR just changed.
    repo_path = _ensure_fresh_repo(pr.project.owner, pr.project.repo)

    # Build the project's ForgeClient so the diff is fetched from the
    # configured forge (matching the poll path), not hard-coded public GitHub.
    forge_client = _forge_client_for(project_config, operator_config)

    log_lines: list[str] = []
    drafts = process_pr(
        pr,
        project_config,
        operator_config,
        repo_path=repo_path,
        force=True,
        log_lines=log_lines,
        forge_client=forge_client,
    )
    summary = f"Generated {len(drafts)} finding(s)"
    if log_lines:
        joined = "\n".join(log_lines[-50:])  # cap log size
        cmd.log = f"{summary}\n{joined}"
    else:
        cmd.log = summary
