"""
Daily digest service stub.

Clear module boundary for future email digest implementation.
For now, this just collects the data that would go into a digest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from franktheunicorn.core.models import PullRequest, ReviewDraft


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
        .order_by("-interest_score")
    )

    entries: list[DigestEntry] = []
    total_drafts = 0

    for pr in recent_prs:
        pending = ReviewDraft.objects.filter(pull_request=pr, status="pending").count()
        total_drafts += pending
        entries.append(
            DigestEntry(
                pr_number=pr.number,
                pr_title=pr.title,
                project_name=pr.project.full_name,
                interest_score=pr.interest_score,
                pending_drafts=pending,
                url=pr.url,
            )
        )

    return DailyDigest(
        entries=entries,
        total_prs_reviewed=len(entries),
        total_drafts_pending=total_drafts,
    )
