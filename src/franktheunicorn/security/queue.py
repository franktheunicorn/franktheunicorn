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
    from franktheunicorn.core.models import SecurityReport

logger = logging.getLogger(__name__)

_IN_FLIGHT_STATUSES = ("pending", "running")


def queue_triage(report: SecurityReport) -> bool:
    """Queue triage for *report* unless a run is already waiting or in flight.

    Returns True if a new command was created.

    Auto-triage and the operator's Triage button are two doors to the same
    work, and the button is the obvious next move on a report whose auto-triage
    hasn't landed yet — so without this, a click (or a double-click) meant two
    NVD lookups and two pairs of LLM calls, with the second overwriting the
    first's verdict. The DB constraint is what actually enforces it; the query
    below just avoids relying on an IntegrityError for the common case.
    """
    if _in_flight(report):
        return False

    try:
        with transaction.atomic():
            WorkerCommand.objects.create(command="run_security_triage", security_report=report)
    except IntegrityError:
        # Lost the race against a concurrent request; the other one queued it.
        logger.debug("Triage already queued for report #%d (constraint)", report.pk)
        return False
    return True


def _in_flight(report: SecurityReport) -> bool:
    return WorkerCommand.objects.filter(
        command="run_security_triage",
        security_report=report,
        status__in=_IN_FLIGHT_STATUSES,
    ).exists()
