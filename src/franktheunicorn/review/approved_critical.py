"""The "committer approved it but we found something critical" flag.

A PR with a committer's approval is probably close to merging — which is
exactly when a critical finding matters most: the author and reviewers have
stopped looking, and the remaining step is the merge. After every agent run
that leaves drafts, the worker compares the PR's approvals against the
project's ``committers`` list and its live drafts' severities, and flags the
PR when both sides are present.

Not a routing queue: like the almost-ready verdict it cuts across the
moderation queues, and the dashboard tab filters on the boolean. The flag is
recomputed from current state on every run, so a withdrawn approval, a
rejected draft, or a force-run that finds nothing clears it as readily as it
was set.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)

#: Severities that count as "potentially critical". Only ``critical`` — an
#: ``important`` finding is a everyday review outcome, and this list is meant
#: to stay rare enough that every row in the tab deserves a look today.
FLAG_SEVERITIES: tuple[str, ...] = ("critical",)


def update_approved_critical(pr: PullRequest, pc: ProjectConfig) -> bool:
    """Recompute the flag from the stored row and live drafts. Returns it.

    Without a ``committers`` list the answer is unknowable — the flag is
    cleared rather than left stale, since the config that would justify it
    has been removed.
    """
    from django.utils import timezone

    from franktheunicorn.core.models import PullRequest as PullRequestModel
    from franktheunicorn.core.models import ReviewDraft

    committers = {c.lower() for c in pc.committers}
    flagged = False
    detail: dict[str, object] = {}
    if committers:
        approvers = sorted(
            login
            for login, review in (pr.review_states or {}).items()
            if review.get("state") == "APPROVED" and login.lower() in committers
        )
        if approvers:
            drafts = ReviewDraft.objects.filter(
                pull_request=pr, severity__in=FLAG_SEVERITIES
            ).exclude(status="rejected")
            draft_ids = list(drafts.values_list("pk", flat=True))
            if draft_ids:
                flagged = True
                detail = {
                    "approvers": approvers,
                    "draft_ids": draft_ids,
                    "at": timezone.now().isoformat(),
                }
    pr.approved_critical = flagged
    pr.approved_critical_detail = detail
    PullRequestModel.objects.filter(pk=pr.pk).update(
        approved_critical=flagged, approved_critical_detail=detail
    )
    return flagged
