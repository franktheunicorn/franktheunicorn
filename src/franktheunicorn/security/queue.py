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
    except IntegrityError:
        target = f"report #{report.pk}" if report is not None else f"PR #{pull_request.pk}"  # type: ignore[union-attr]
        logger.info("%s already queued for %s", command, target)
        return False
    return True


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
