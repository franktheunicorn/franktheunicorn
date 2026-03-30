"""
Daily digest service stub.

Clear module boundary for future email digest implementation.
For now, this just collects the data that would go into a digest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from django.db.models import Count, Q

from franktheunicorn.core.models import PullRequest


@dataclass
class DigestEntry:
    """A single PR entry in the daily digest."""

    pr_number: int
    pr_title: str
    project_name: str
    interest_score: float
    pending_drafts: int
    url: str


@dataclass
class DailyDigest:
    """The full daily digest content."""

    generated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    entries: list[DigestEntry] = field(default_factory=list)
    total_prs_reviewed: int = 0
    total_drafts_pending: int = 0


def build_daily_digest(hours: int = 24) -> DailyDigest:
    """
    Build a digest of PR activity from the last N hours.

    Returns structured data that can be rendered as email, HTML, or CLI output.
    """
    cutoff = datetime.now(tz=UTC) - timedelta(hours=hours)
    recent_prs = (
        PullRequest.objects.filter(updated_at__gte=cutoff, state="open")
        .select_related("project")
        .annotate(_pending=Count("review_drafts", filter=Q(review_drafts__status="pending")))
        .order_by("-interest_score")
    )

    entries = [
        DigestEntry(
            pr_number=pr.number,
            pr_title=pr.title,
            project_name=pr.project.full_name,
            interest_score=pr.interest_score,
            pending_drafts=pr._pending,
            url=pr.url,
        )
        for pr in recent_prs
    ]

    return DailyDigest(
        entries=entries,
        total_prs_reviewed=len(entries),
        total_drafts_pending=sum(e.pending_drafts for e in entries),
    )
