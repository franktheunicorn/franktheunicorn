"""Queueing for security-report triage.

One door for every caller — the paste form, the dashboard's Triage button, and
email ingestion. Triage is worker work (an NVD lookup plus two LLM calls), and
whoever asks for it should only ever be creating a ``WorkerCommand`` row.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from franktheunicorn.core.models import WorkerCommand

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig
    from franktheunicorn.core.models import SecurityReport

logger = logging.getLogger(__name__)

_IN_FLIGHT_STATUSES = ("pending", "running")


def in_flight_statuses() -> tuple[str, ...]:
    """The statuses that mean a triage run is already under way.

    Exposed so the dashboard can ask rather than hardcode the two literals in a
    template, where they'd drift out of step with the partial unique constraint
    that enforces them.
    """
    return _IN_FLIGHT_STATUSES


def queue_triage(report: SecurityReport) -> bool:
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
    return queue_command("run_security_triage", report)


def queue_command(command: str, report: SecurityReport) -> bool:
    """Queue *command* for *report* unless one is already waiting or running.

    The generic form of :func:`queue_triage`. The partial unique constraint
    ``unique_inflight_command_per_report`` covers ``(command, security_report)``
    for *every* command type, not just triage — so ``security_report_sandbox``
    creating a WorkerCommand directly was an uncaught IntegrityError, and a 500 on
    the second click. htmx doesn't swap on a 5xx, so the operator saw the button
    do nothing and clicked again. This is the door CLAUDE.md's "never straight to
    WorkerCommand" rule wants to be structural rather than conventional.
    """
    # The constraint is the check. There used to be a pre-flight SELECT here to
    # avoid relying on an IntegrityError for the common case, and it cost more than
    # it saved: a bulk import calls this on a row it created microseconds earlier,
    # which cannot have a command, so a 2000-entry archive with --triage ran 2000
    # provably-empty SELECTs inside the single write transaction the design is
    # built around. The semantics are identical either way — already-in-flight
    # returns False from the constraint just as it did from the query.
    try:
        with transaction.atomic():
            WorkerCommand.objects.create(command=command, security_report=report)
    except IntegrityError:
        logger.info("%s already queued for report #%d", command, report.pk)
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
    return queue_triage(report)


def queue_triage_on_request(
    report: SecurityReport, operator_config: OperatorConfig | None = None
) -> bool:
    """Queue triage because the operator asked for it, not because ingest did.

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
    return queue_triage(report)


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
