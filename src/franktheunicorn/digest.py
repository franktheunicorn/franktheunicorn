"""Daily digest plumbing.

Stub module with clear interface boundaries so email/digest functionality
can be wired in later without touching the rest of the system.

For v0, ``build_digest`` returns a structured dict that can be rendered
to stdout or (later) formatted into an email or Slack message.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy.orm import Session

from franktheunicorn.models import PullRequest
from franktheunicorn.storage import list_pull_requests

logger = logging.getLogger(__name__)


def build_digest(session: Session, since_hours: int = 24) -> dict[str, Any]:
    """Build a daily digest of interesting PRs.

    Returns a dict suitable for rendering in any output format.
    This is intentionally transport-agnostic - the caller decides
    whether to print, email, or post it somewhere.
    """
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=since_hours)
    all_prs: list[PullRequest] = list_pull_requests(session, state="open", limit=200)

    # Filter to PRs updated or ingested recently.
    # SQLite may return timezone-naive datetimes; normalise before comparing.
    def _aware(dt: datetime.datetime) -> datetime.datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.UTC)
        return dt

    recent: list[PullRequest] = [
        pr
        for pr in all_prs
        if (pr.github_updated_at and _aware(pr.github_updated_at) >= cutoff)
        or (pr.ingested_at and _aware(pr.ingested_at) >= cutoff)
    ]

    high_priority = [pr for pr in recent if pr.interest_score >= 1.0]
    medium_priority = [pr for pr in recent if 0.5 <= pr.interest_score < 1.0]

    digest: dict[str, Any] = {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "since_hours": since_hours,
        "total_recent_prs": len(recent),
        "high_priority": [_pr_summary(pr) for pr in high_priority],
        "medium_priority": [_pr_summary(pr) for pr in medium_priority],
    }
    logger.info(
        "Digest built: %d recent PRs (%d high, %d medium)",
        len(recent),
        len(high_priority),
        len(medium_priority),
    )
    return digest


def _pr_summary(pr: PullRequest) -> dict[str, Any]:
    return {
        "id": pr.id,
        "pr_number": pr.github_pr_number,
        "title": pr.title,
        "author": pr.author_login,
        "url": pr.html_url,
        "score": pr.interest_score,
    }


def send_digest(digest: dict[str, Any]) -> None:
    """Send the digest somewhere.

    Stub: logs to stdout for now.  Replace with SMTP / webhook later.
    """
    logger.info("=== Daily Digest (%d recent PRs) ===", digest.get("total_recent_prs", 0))
    for pr in digest.get("high_priority", []):
        logger.info("[HIGH] #%s %s (score=%.2f)", pr["pr_number"], pr["title"], pr["score"])
    for pr in digest.get("medium_priority", []):
        logger.info("[MED]  #%s %s (score=%.2f)", pr["pr_number"], pr["title"], pr["score"])
