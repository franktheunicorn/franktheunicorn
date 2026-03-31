"""
Daily digest service — builds, renders, and sends email digests.

Supports plain text and HTML rendering. SMTP sending via Django email.
Workspace-aware: can filter by workspace or show all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum

from franktheunicorn.core.models import (
    AntiPattern,
    CostRecord,
    PullRequest,
    ReviewDraft,
    TestRun,
)

logger = logging.getLogger(__name__)


@dataclass
class DigestEntry:
    """A single PR entry in the daily digest."""

    pr_number: int
    pr_title: str
    project_name: str
    interest_score: float
    pending_drafts: int
    url: str
    queue: str = "review"
    test_verdict: str = ""


@dataclass
class DigestStats:
    """Weekly/monthly stats for digest."""

    prs_reviewed: int = 0
    findings_posted: int = 0
    accuracy_pct: float = 0.0
    total_cost_usd: Decimal = Decimal("0")
    container_minutes: float = 0.0
    anti_patterns_suppressed: int = 0
    stale_anti_patterns: int = 0


@dataclass
class DailyDigest:
    """The full daily digest content."""

    generated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    high_interest: list[DigestEntry] = field(default_factory=list)
    your_prs: list[DigestEntry] = field(default_factory=list)
    ai_generated: list[DigestEntry] = field(default_factory=list)
    moderation: list[DigestEntry] = field(default_factory=list)
    test_issues: list[DigestEntry] = field(default_factory=list)
    stats: DigestStats = field(default_factory=DigestStats)

    # Legacy compat
    entries: list[DigestEntry] = field(default_factory=list)
    total_prs_reviewed: int = 0
    total_drafts_pending: int = 0


def build_daily_digest(hours: int = 24) -> DailyDigest:
    """Build a digest of PR activity from the last N hours."""
    cutoff = datetime.now(tz=UTC) - timedelta(hours=hours)
    recent_prs = (
        PullRequest.objects.filter(updated_at__gte=cutoff, state="open")
        .select_related("project")
        .annotate(_pending=Count("review_drafts", filter=Q(review_drafts__status="pending")))
        .order_by("-interest_score")
    )

    def _to_entry(pr: PullRequest) -> DigestEntry:
        # Get latest test verdict if any.
        test_verdict = ""
        latest_run = TestRun.objects.filter(pull_request=pr).order_by("-created_at").first()
        if latest_run and latest_run.differential_verdict:
            test_verdict = latest_run.differential_verdict

        return DigestEntry(
            pr_number=pr.number,
            pr_title=pr.title,
            project_name=pr.project.full_name,
            interest_score=pr.interest_score,
            pending_drafts=pr._pending,  # type: ignore[attr-defined]
            url=pr.url,
            queue=pr.queue,
            test_verdict=test_verdict,
        )

    digest = DailyDigest()

    for pr in recent_prs:
        entry = _to_entry(pr)
        digest.entries.append(entry)

        if pr.interest_score >= 0.7:
            digest.high_interest.append(entry)
        if pr.queue == "your-prs":
            digest.your_prs.append(entry)
        if pr.queue == "ai-generated":
            digest.ai_generated.append(entry)
        if pr.queue in ("consider-closing", "needs-triage"):
            digest.moderation.append(entry)
        if entry.test_verdict in ("suspect", "broken"):
            digest.test_issues.append(entry)

    digest.total_prs_reviewed = len(digest.entries)
    digest.total_drafts_pending = sum(e.pending_drafts for e in digest.entries)

    # Weekly stats (always included, the template decides when to show them).
    week_cutoff = datetime.now(tz=UTC) - timedelta(days=7)
    from franktheunicorn.core.models import OperatorAction

    actions_7d = OperatorAction.objects.filter(created_at__gte=week_cutoff)
    accepted = actions_7d.filter(action_type="accept_draft").count()
    rejected = actions_7d.filter(action_type="reject_draft").count()
    edited = actions_7d.filter(action_type="edit_draft").count()
    total_actions = accepted + rejected + edited

    cost_7d = CostRecord.objects.filter(created_at__gte=week_cutoff).aggregate(
        total=Sum("estimated_cost_usd"),
    )

    posted_7d = ReviewDraft.objects.filter(
        status="posted",
        posted_at__gte=week_cutoff,
    ).count()

    ap_suppressed = AntiPattern.objects.filter(
        is_active=True,
    ).aggregate(total=Sum("times_triggered"))

    stale_aps = (
        AntiPattern.objects.filter(
            is_active=True,
        )
        .filter(
            Q(last_matched_at__lt=datetime.now(tz=UTC) - timedelta(days=60))
            | Q(last_matched_at__isnull=True),
        )
        .count()
    )

    digest.stats = DigestStats(
        prs_reviewed=total_actions,
        findings_posted=posted_7d,
        accuracy_pct=round(accepted / total_actions * 100, 1) if total_actions else 0.0,
        total_cost_usd=cost_7d.get("total") or Decimal("0"),
        anti_patterns_suppressed=ap_suppressed.get("total") or 0,
        stale_anti_patterns=stale_aps,
    )

    return digest


def render_digest_text(digest: DailyDigest) -> str:
    """Render digest as plain text email body."""
    lines: list[str] = []
    now = digest.generated_at.strftime("%b %d, %Y")
    total = digest.total_prs_reviewed
    lines.append(f"franktheunicorn digest — {now} — {total} PRs need attention")
    lines.append("")

    if digest.high_interest:
        lines.append("HIGH-INTEREST PRs (score >= 0.7)")
        for e in digest.high_interest:
            test_str = f" Tests: {e.test_verdict}" if e.test_verdict else ""
            lines.append(
                f'  {e.project_name}#{e.pr_number} — "{e.pr_title}"'
                f"\n  score: {e.interest_score:.2f} · {e.pending_drafts} drafts{test_str}"
            )
        lines.append("")

    if digest.your_prs:
        lines.append("YOUR PRs NEEDING ACTION")
        for e in digest.your_prs:
            lines.append(f'  {e.project_name}#{e.pr_number} — "{e.pr_title}"')
        lines.append("")

    if digest.ai_generated:
        lines.append("AI-GENERATED PRs")
        for e in digest.ai_generated:
            lines.append(
                f'  {e.project_name}#{e.pr_number} — "{e.pr_title}"\n  {e.pending_drafts} drafts'
            )
        lines.append("")

    if digest.moderation:
        lines.append("MODERATION")
        for e in digest.moderation:
            lines.append(f"  {e.project_name}#{e.pr_number} — {e.queue}")
        lines.append("")

    if digest.test_issues:
        lines.append("TEST ISSUES")
        for e in digest.test_issues:
            lines.append(f"  {e.project_name}#{e.pr_number} — {e.test_verdict}")
        lines.append("")

    if digest.stats.prs_reviewed > 0:
        s = digest.stats
        lines.append("WEEKLY STATS")
        lines.append(
            f"  Reviewed: {s.prs_reviewed} · Posted: {s.findings_posted}"
            f" · Accuracy: {s.accuracy_pct:.0f}% as-is"
        )
        lines.append(f"  Cost: ~${s.total_cost_usd:.2f} LLM")
        if s.anti_patterns_suppressed:
            lines.append(f"  Anti-patterns suppressed {s.anti_patterns_suppressed} findings")
        if s.stale_anti_patterns:
            lines.append(f"  {s.stale_anti_patterns} anti-patterns haven't matched in 60 days")
        lines.append("")

    return "\n".join(lines)


def render_digest_html(digest: DailyDigest) -> str:
    """Render digest as HTML email body."""
    from django.template.loader import render_to_string

    return render_to_string("digest/email.html", {"digest": digest})


def send_digest(digest: DailyDigest | None = None) -> bool:
    """Send the daily digest email.

    Returns True if the email was sent, False if skipped (no email configured).
    """
    from django.conf import settings
    from django.core.mail import send_mail

    email_to = getattr(settings, "FRANK_DIGEST_EMAIL", "")
    if not email_to:
        logger.info("Digest email not configured (FRANK_DIGEST_EMAIL empty). Skipping.")
        return False

    if digest is None:
        digest = build_daily_digest()

    subject = (
        f"franktheunicorn digest — {digest.generated_at.strftime('%b %d, %Y')}"
        f" — {digest.total_prs_reviewed} PRs need attention"
    )
    text_body = render_digest_text(digest)

    try:
        html_body = render_digest_html(digest)
    except Exception:
        logger.debug("HTML digest rendering failed; sending text-only.", exc_info=True)
        html_body = ""

    try:
        send_mail(
            subject=subject,
            message=text_body,
            html_message=html_body or None,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "frank@localhost"),
            recipient_list=[email_to],
            fail_silently=False,
        )
        logger.info("Digest sent to %s", email_to)
        return True
    except Exception:
        logger.exception("Failed to send digest email to %s", email_to)
        return False
