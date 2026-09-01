"""Queueing for worker commands — security-report triage and the PR buttons.

One door for every caller — the paste form, the dashboard's Triage button, email
ingestion, and the PR detail page's Force Run / Run Dual Tests. All of it is
worker work (LLM calls, NVD lookups, Docker), and whoever asks for it should only
ever be creating a ``WorkerCommand`` row.

Two policies live here rather than at each call site:

* **In-flight dedup**, enforced by a partial unique constraint per target, so a
  double-click is one run rather than two.
* **Priority**, because the queue is shared. A bulk import can put a thousand
  triage rows in it, and a FIFO queue then makes the operator's click wait behind
  all of them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from franktheunicorn.core.models import WorkerCommand

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig
    from franktheunicorn.core.models import PullRequest, SecurityReport

logger = logging.getLogger(__name__)

_IN_FLIGHT_STATUSES = ("pending", "running")

#: Someone is sitting in front of the dashboard waiting for this one.
PRIORITY_INTERACTIVE = 100

#: Queued by ingestion or a bulk import. Nobody is watching; it can wait.
PRIORITY_BULK = 0


def in_flight_statuses() -> tuple[str, ...]:
    """The statuses that mean a triage run is already under way.

    Exposed so the dashboard can ask rather than hardcode the two literals in a
    template, where they'd drift out of step with the partial unique constraint
    that enforces them.
    """
    return _IN_FLIGHT_STATUSES


def queue_triage(report: SecurityReport, *, priority: int = PRIORITY_BULK) -> bool:
    """Queue triage for *report* unless a run is already waiting or in flight.

    Returns True if a new command was created.

    Auto-triage and the operator's Triage button are two doors to the same
    work, and the button is the obvious next move on a report whose auto-triage
    hasn't landed yet — so without this, a click (or a double-click) meant two
    NVD lookups and two pairs of LLM calls, with the second overwriting the
    first's verdict.

    Delegates rather than repeating ``queue_command`` with the command name fixed.
    The two had already drifted — different log levels for the same event — which
    is how one copy of a policy in a module whose whole point is to be the single
    door gets fixed and the other doesn't.
    """
    return queue_command("run_security_triage", report=report, priority=priority)


def queue_verification(report: SecurityReport, *, priority: int = PRIORITY_BULK) -> bool:
    """Queue the deep verifier for one report unless a run is already in flight.

    Same door, same dedup, same priority rules as everything else here — a
    verification is minutes of agent time per branch, so a double-click producing
    two of them is worse than for any other command in this queue.
    """
    return queue_command("verify_security_report", report=report, priority=priority)


def queue_version_map(report: SecurityReport, *, priority: int = PRIORITY_BULK) -> bool:
    """Queue the cheap version-mapper: cited files and whether the patch applies.

    Git only — no agent — so a 143-report archive is minutes, not thousands of
    hours. Still goes through this door so a double-click is one run.
    """
    return queue_command("map_report_versions", report=report, priority=priority)


def queue_introduction_scan(report: SecurityReport, *, priority: int = PRIORITY_BULK) -> bool:
    """Queue the git-history dating pass: which commit, and which releases have it.

    Git only, like the version mapper — a pickaxe walk per cited path rather than
    an agent. Same door, so a double-click is one run.
    """
    return queue_command("find_report_introduction", report=report, priority=priority)


def _verifier_gate_reason(operator_config: OperatorConfig) -> str:
    """Why the deep verifier (and the version map, which shares its checkout)
    can't run, or "" when they can.

    The two checks ``verifier.enabled`` and a resolvable reviewer, the same two
    zip_import's ``_queue_verifications``/``_queue_git_scan`` make. Lifted here
    so the worker's triage follow-on and the dashboard's bulk re-triage button
    get the same answer without each re-deriving it — and without queuing a
    command the worker would no-op on, which is noise that reads as "the button
    did nothing".
    """
    verifier = operator_config.security_triage.verifier
    if not verifier.enabled:
        return (
            "security_triage.verifier.enabled is false in operator.yaml — it defaults "
            "true, so either something set it or the config failed to load and took "
            "every other setting with it (`manage.py show_config` tells those apart)"
        )
    from franktheunicorn.security.verifier import resolve_verifier_reviewer

    if resolve_verifier_reviewer(operator_config, verifier) is None:
        have = ", ".join(rc.name for rc in operator_config.agent_cli_reviewers) or "none"
        return (
            f"no agent_cli_reviewers entry named {verifier.reviewer!r} (configured: "
            f"{have}), so there is no coding-agent CLI / checkout to run"
        )
    return ""


def queue_version_follow_on(
    report: SecurityReport,
    operator_config: OperatorConfig,
    *,
    priority: int = PRIORITY_BULK,
) -> tuple[bool, bool, str]:
    """Queue the cheap version map and the deep verifier for *report*.

    Both answer "where does this hole live across release branches" — the
    version map with git alone (cited files + whether the proposed patch
    applies), the verifier with a coding agent per branch. Used after triage
    rules a report valid-looking (the worker chains it) and from the bulk
    re-triage button for operator-ruled-valid reports that haven't written
    down affected versions.

    Honours the verifier gate (``verifier.enabled`` and a configured reviewer)
    and the project requirement: a report with no project has no repo to check,
    and queuing a command the worker will no-op on is noise in the queue.
    Returns ``(version_map_queued, verify_queued, skipped_reason)`` — the bools
    are False and the reason set when the gate stopped either.
    """
    if report.project_id is None:
        return False, False, "report has no project, so there is no repo to check it against"
    reason = _verifier_gate_reason(operator_config)
    if reason:
        return False, False, reason
    version_map_queued = queue_version_map(report, priority=priority)
    verify_queued = queue_verification(report, priority=priority)
    logger.info(
        "Queued version follow-on for report #%d: version_map=%s verify=%s",
        report.pk,
        version_map_queued,
        verify_queued,
    )
    return version_map_queued, verify_queued, ""


def queue_recheck_poll(*, priority: int = PRIORITY_BULK, exclude_pk: int | None = None) -> bool:
    """Queue the wait on launched recheck runs unless one is already in flight.

    Targetless, so neither per-target unique constraint covers it and the
    in-flight check is a SELECT — the pre-flight read ``queue_command`` dropped
    is fine at this rate: one row per button press, not two thousand per
    import.

    *exclude_pk* is the poll handler re-queueing itself: its own row is still
    ``running`` at that moment and would otherwise count as the in-flight one,
    so every poll after the first would decline and the runs would go unread.
    """
    in_flight = WorkerCommand.objects.filter(
        command="poll_security_rechecks", status__in=_IN_FLIGHT_STATUSES
    )
    if exclude_pk is not None:
        in_flight = in_flight.exclude(pk=exclude_pk)
    if in_flight.exists():
        logger.info("poll_security_rechecks already in flight")
        return False
    WorkerCommand.objects.create(command="poll_security_rechecks", priority=priority)
    logger.info("Queued poll_security_rechecks (priority %d)", priority)
    return True


def cancel_pending_for_reports(report_ids: list[int]) -> int:
    """Drop pending worker jobs for *report_ids* before the reports themselves go.

    CASCADE on report delete would get them eventually, but a worker can claim a
    pending row in the gap and then spend an NVD lookup or an agent run on a
    report that no longer exists. Deleting the pending rows first is the cancel.
    Running ones are already claimed — those finish or fail on the missing row.

    Returns how many command rows were deleted.
    """
    if not report_ids:
        return 0
    deleted, _ = WorkerCommand.objects.filter(
        security_report_id__in=report_ids,
        status="pending",
    ).delete()
    if deleted:
        logger.info("Cancelled %d pending worker command(s) for dropped report(s)", deleted)
    return deleted


def queue_command(
    command: str,
    report: SecurityReport | None = None,
    *,
    pull_request: PullRequest | None = None,
    priority: int = PRIORITY_BULK,
) -> bool:
    """Queue *command* for a report or a PR unless one is already in flight.

    Exactly one of *report* / *pull_request* is the target. Both partial unique
    constraints — ``unique_inflight_command_per_report`` and
    ``unique_inflight_command_per_pr`` — cover ``(command, target)`` for *every*
    command type, so a double-click is one run: ``security_report_sandbox``
    creating a WorkerCommand directly was an uncaught IntegrityError and a 500 on
    the second click, and ``run_agents`` had no constraint at all, so five
    impatient clicks were five sequential 30-120s pipeline runs. This is the door
    CLAUDE.md's "never straight to WorkerCommand" rule wants to be structural
    rather than conventional.

    *priority* orders the queue; see :data:`PRIORITY_INTERACTIVE`.
    """
    if (report is None) == (pull_request is None):
        msg = "queue_command needs exactly one of report / pull_request"
        raise ValueError(msg)
    target = f"report #{report.pk}" if report is not None else f"PR #{pull_request.pk}"  # type: ignore[union-attr]
    # The constraint is the check. There used to be a pre-flight SELECT here to
    # avoid relying on an IntegrityError for the common case, and it cost more than
    # it saved: a bulk import calls this on a row it created microseconds earlier,
    # which cannot have a command, so a 2000-entry archive with --triage ran 2000
    # provably-empty SELECTs inside the single write transaction the design is
    # built around. The semantics are identical either way — already-in-flight
    # returns False from the constraint just as it did from the query.
    try:
        with transaction.atomic():
            WorkerCommand.objects.create(
                command=command,
                security_report=report,
                pull_request=pull_request,
                priority=priority,
            )
    except IntegrityError as exc:
        if not _is_inflight_conflict(exc):
            # Not a duplicate — a foreign key to a row deleted between page render
            # and POST, a NOT NULL violation, anything else. Reporting those as
            # "already queued" sent the operator off to reload forever waiting for
            # a run that was never created, with the real error discarded.
            logger.exception("Could not queue %s for %s", command, target)
            raise
        logger.info("%s already queued for %s", command, target)
        return False
    # The success case was the only one with no line: a click produced nothing
    # in any log until the worker claimed the row, which made "the button did
    # nothing" and "the worker isn't running" indistinguishable from here.
    logger.info("Queued %s for %s (priority %d)", command, target, priority)
    return True


#: The two partial unique constraints whose violation genuinely means
#: "already in flight". Matched by name where the driver gives us one, and by the
#: column pair where it doesn't — SQLite reports
#: ``UNIQUE constraint failed: core_workercommand.command,
#: core_workercommand.pull_request_id`` with no constraint name at all.
_INFLIGHT_CONSTRAINTS = (
    "unique_inflight_command_per_report",
    "unique_inflight_command_per_pr",
)


def _is_inflight_conflict(exc: IntegrityError) -> bool:
    """Whether *exc* is one of our in-flight constraints rather than any old clash."""
    message = str(exc)
    if any(name in message for name in _INFLIGHT_CONSTRAINTS):
        return True
    # SQLite's nameless form: a UNIQUE failure naming both columns of the pair.
    return (
        "UNIQUE constraint failed" in message
        and "command" in message
        and ("pull_request_id" in message or "security_report_id" in message)
    )


def queue_triage_if_enabled(
    report: SecurityReport, operator_config: OperatorConfig | None = None
) -> bool:
    """Queue triage for *report* only when the operator turned auto-triage on.

    Two settings — ``enabled`` and ``auto_triage`` — and each ingest door was
    spelling the check out for itself: the paste form tested both, the email
    poller tested only ``auto_triage`` and relied on an early return higher up
    for the other. That worked, but it left the next door (this zip import) to
    rediscover which halves it needed. One helper, one answer.

    Loads the operator config when the caller doesn't already have it.
    """
    triage_config = _resolve(operator_config).security_triage
    if not triage_config.enabled or not triage_config.auto_triage:
        # A gate that stops configured work says so, naming the setting that
        # changes it — the silent version of this is "reports sit in new
        # forever and nobody knows why". The callers are the trickle paths
        # (paste form, inbox poll), so one line per report is affordable.
        logger.info(
            "Not auto-triaging report #%d: security_triage.enabled=%s, "
            "security_triage.auto_triage=%s (both in operator.yaml).",
            report.pk,
            triage_config.enabled,
            triage_config.auto_triage,
        )
        return False
    return queue_triage(report, priority=PRIORITY_BULK)


def queue_triage_on_request(
    report: SecurityReport,
    operator_config: OperatorConfig | None = None,
    *,
    priority: int = PRIORITY_BULK,
) -> bool:
    """Queue triage because the operator asked for it, not because ingest did.

    *priority* is the caller's, not this function's: the bulk importer asks on
    the operator's behalf for a thousand reports at once (bulk), while the
    detail page's Triage button is one report with someone watching
    (:data:`PRIORITY_INTERACTIVE`). Defaults to bulk, because the loud caller is
    the import.

    Ungated, deliberately. Both settings describe *automatic* behaviour:
    ``auto_triage`` is "triage things as they arrive", and ``enabled`` switches on
    the machinery that makes them arrive (inbox polling, ingest hooks). Neither
    answers "the operator is looking at this report and pressed the button".

    Gating this on ``enabled`` looked like a consistency win and was a real
    regression: it defaults to False and ``config/examples/operator.yaml`` ships
    the whole ``security_triage:`` block commented out, so on the install the
    documented setup actually produces, the button became a permanent no-op
    pointing at a key that is not in the operator's file — while the security
    page, the paste form and the nav link all stayed reachable. A visible button
    on a page you navigated to is the consent. Callers still check
    ``llm_backends``, which is what genuinely has to be there.
    """
    del operator_config  # nothing to gate on; kept for call-site symmetry
    return queue_triage(report, priority=priority)


def _resolve(operator_config: OperatorConfig | None) -> OperatorConfig:
    """The caller's config, or a freshly loaded one.

    Callers in a loop should pass their own: ``get_operator_config`` is not
    cached, so leaving this to re-read per report reparses the YAML every time
    and lets a mid-import edit split-brain the archive.
    """
    if operator_config is not None:
        return operator_config
    from franktheunicorn.config.loader import get_operator_config

    return get_operator_config()
