"""Alert mode — detection and email delivery.

Watches for two conditions:

1. **Working overlap** — someone else raises a PR that matches work the
   operator has in flight: it touches the same files as one of the
   operator's own open PRs, or it matches the project's declared
   ``alerts.working_paths`` / ``alerts.working_keywords``.
2. **Security report waiting** — a ``SecurityReport`` is in the queue
   (status ``new``) or in triage (status ``triaging``).

Detection runs from the worker poll cycle (see ``run_alert_sweep``).
Every alert is recorded as an ``Alert`` row — the unique ``dedup_key``
guarantees each PR/report alerts at most once — and all alerts not yet
emailed are batched into a single email per cycle.

Graceful degradation, per project convention: the feature is inert until
the operator sets ``alerts.enabled`` in operator.yaml; with no recipient
(``alerts.email`` falling back to ``digest_email``) alerts are recorded
but no email is sent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Alert, Project, PullRequest, SecurityReport
from franktheunicorn.scoring.signals import path_matches

if TYPE_CHECKING:
    from collections.abc import Sequence

    from franktheunicorn.config.models import OperatorConfig

logger = logging.getLogger(__name__)

# Security report statuses that mean "waiting for the operator": sitting
# in the queue (new) or actively in triage.
ALERTABLE_REPORT_STATUSES: tuple[str, ...] = ("new", "triaging")

# Cap file lists embedded in reason strings so emails stay readable.
_MAX_REASON_FILES = 5


def _format_file_list(files: list[str]) -> str:
    shown = ", ".join(files[:_MAX_REASON_FILES])
    extra = len(files) - _MAX_REASON_FILES
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _single_line(text: str) -> str:
    """Collapse all whitespace (including CR/LF) into single spaces.

    Alert titles end up in email Subject headers, and Django raises
    BadHeaderError on newlines there — which would strand the alert
    unsent and retried forever.
    """
    return " ".join(text.split())


def find_working_overlap_reasons(pr: PullRequest, project_config: ProjectConfig) -> list[str]:
    """Explain how ``pr`` overlaps the operator's in-flight work.

    Returns human-readable reason strings; empty list means no overlap.
    Checks, in order: file overlap with the operator's own open PRs in
    the same project, ``alerts.working_paths`` matches, and
    ``alerts.working_keywords`` matches in the title/body.
    """
    reasons: list[str] = []
    changed = [f for f in (pr.changed_files or []) if f]
    alerts_config = project_config.alerts

    if changed:
        operator_prs = PullRequest.objects.filter(
            project=pr.project, state="open", is_operator_pr=True
        ).exclude(pk=pr.pk)
        for op_pr in operator_prs:
            overlap = sorted(set(changed) & {f for f in (op_pr.changed_files or []) if f})
            if overlap:
                reasons.append(
                    f"touches files also changed by your open PR #{op_pr.number}"
                    f' ("{op_pr.title}"): {_format_file_list(overlap)}'
                )

        if alerts_config.working_paths:
            matched = sorted(
                f for f in changed if any(path_matches(f, p) for p in alerts_config.working_paths)
            )
            if matched:
                reasons.append(f"touches your working paths: {_format_file_list(matched)}")

    if alerts_config.working_keywords:
        text = f"{pr.title}\n{pr.body}".lower()
        matched_keywords = [kw for kw in alerts_config.working_keywords if kw.lower() in text]
        if matched_keywords:
            reasons.append(f"mentions your working keywords: {', '.join(matched_keywords)}")

    return reasons


def _record_alert(
    *,
    alert_type: str,
    dedup_key: str,
    title: str,
    reasons: list[str],
    project: Project | None = None,
    pull_request: PullRequest | None = None,
    security_report: SecurityReport | None = None,
) -> Alert | None:
    """Create an Alert unless one already exists for ``dedup_key``."""
    alert, created = Alert.objects.get_or_create(
        dedup_key=dedup_key,
        defaults={
            "alert_type": alert_type,
            "title": title,
            "reasons": reasons,
            "project": project,
            "pull_request": pull_request,
            "security_report": security_report,
        },
    )
    if not created:
        return None
    logger.info("Alert raised [%s]: %s", alert_type, title)
    return alert


def sweep_pr_alerts(
    project_configs: Sequence[object],
    operator_config: OperatorConfig,
) -> list[Alert]:
    """Raise working-overlap alerts for open PRs across alert-enabled projects.

    DB-driven so it covers every ingestion path (poll loop, backfill,
    single-PR ingest). The ``Alert`` dedup ledger keeps re-sweeps cheap
    and idempotent. Returns the newly created alerts.
    """
    if not operator_config.alerts.enabled:
        return []

    created: list[Alert] = []
    for pc in project_configs:
        if not isinstance(pc, ProjectConfig) or not pc.enabled:
            continue
        if not (pc.alerts.enabled and pc.alerts.working_overlap):
            continue
        project = Project.objects.filter(owner=pc.owner, repo=pc.repo).first()
        if project is None:
            continue

        candidates = (
            PullRequest.objects.filter(project=project, state="open", is_operator_pr=False)
            .exclude(alerts__alert_type="working-overlap")
            .order_by("number")
        )
        for pr in candidates:
            try:
                reasons = find_working_overlap_reasons(pr, pc)
            except Exception:
                logger.exception("Working-overlap check failed for PR #%d", pr.number)
                continue
            if not reasons:
                continue
            alert = _record_alert(
                alert_type="working-overlap",
                dedup_key=f"working-overlap:pr:{pr.pk}",
                title=f'{project.full_name}#{pr.number} by {pr.author}: "{pr.title}"',
                reasons=reasons,
                project=project,
                pull_request=pr,
            )
            if alert is not None:
                created.append(alert)
    return created


def sweep_security_report_alerts(
    project_configs: Sequence[object],
    operator_config: OperatorConfig,
) -> list[Alert]:
    """Raise alerts for security reports in the queue or in triage.

    Reports attached to a configured project honour that project's
    ``enabled`` flag and ``alerts`` opt-outs — a disabled project is
    silent here just like everywhere else in the worker. Reports with no
    project (or a project without a config) are governed by the
    operator-level toggle alone. Returns the newly created alerts.
    """
    if not (operator_config.alerts.enabled and operator_config.alerts.security_reports):
        return []

    config_by_project = {
        pc.full_name: pc for pc in project_configs if isinstance(pc, ProjectConfig)
    }

    candidates = (
        SecurityReport.objects.filter(status__in=ALERTABLE_REPORT_STATUSES)
        .exclude(alerts__alert_type="security-report")
        .select_related("project")
        .order_by("created_at")
    )

    created: list[Alert] = []
    for report in candidates:
        if report.project is not None:
            pc = config_by_project.get(report.project.full_name)
            if pc is not None and not (
                pc.enabled and pc.alerts.enabled and pc.alerts.security_reports
            ):
                continue

        where = "in the queue" if report.status == "new" else "in triage"
        # Pasted reports may have no title yet; fall back to the raw text,
        # flattened first so a multi-line paste yields a usable one-liner.
        title_text = _single_line(report.title) or _single_line(report.raw_text)[:80]
        reasons = [f"status: {report.status} ({where})", f"source: {report.source}"]
        if report.assessed_severity and report.assessed_severity != "unknown":
            reasons.append(f"assessed severity: {report.assessed_severity}")
        if report.project is not None:
            reasons.append(f"project: {report.project.full_name}")

        alert = _record_alert(
            alert_type="security-report",
            dedup_key=f"security-report:report:{report.pk}",
            title=f"Security report {where}: {title_text}",
            reasons=reasons,
            project=report.project,
            security_report=report,
        )
        if alert is not None:
            created.append(alert)
    return created


def alert_email_recipient(operator_config: OperatorConfig) -> str:
    """Resolve the alert email recipient (alerts.email, then digest_email)."""
    return operator_config.alerts.email.strip() or operator_config.digest_email.strip()


def render_alert_email_text(alerts: Sequence[Alert]) -> str:
    """Render alerts as a plain-text email body."""
    type_labels = dict(Alert.ALERT_TYPE_CHOICES)
    lines: list[str] = [
        f"frank the unicorn spotted {len(alerts)} thing(s) that need your attention:",
        "",
    ]
    for alert in alerts:
        lines.append(f"[{type_labels.get(alert.alert_type, alert.alert_type)}] {alert.title}")
        lines.extend(f"  - {reason}" for reason in alert.reasons)
        if alert.pull_request is not None and alert.pull_request.url:
            lines.append(f"  {alert.pull_request.url}")
        lines.append("")
    return "\n".join(lines)


def send_pending_alert_emails(operator_config: OperatorConfig) -> int:
    """Email all not-yet-sent alerts as one batched message.

    Returns the number of alerts emailed. Skips silently (alerts stay
    recorded, marked unsent) when no recipient is configured; they are
    picked up by a later sweep once email is configured.
    """
    from django.conf import settings
    from django.core.mail import send_mail

    if not operator_config.alerts.enabled:
        return 0

    pending = list(
        Alert.objects.filter(email_sent=False).select_related("pull_request").order_by("created_at")
    )
    if not pending:
        return 0

    recipient = alert_email_recipient(operator_config)
    if not recipient:
        logger.info(
            "%d alert(s) recorded but no alert email configured "
            "(set alerts.email or digest_email). Skipping send.",
            len(pending),
        )
        return 0

    if len(pending) == 1:
        # Defensively flatten the title: a newline in an email Subject
        # raises BadHeaderError, stranding the alert unsent forever.
        subject = f"[frank alert] {_single_line(pending[0].title)}"
    else:
        subject = f"[frank alert] {len(pending)} new alerts"

    try:
        send_mail(
            subject=subject,
            message=render_alert_email_text(pending),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "frank@localhost"),
            recipient_list=[recipient],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send alert email to %s", recipient)
        return 0

    now = timezone.now()
    for alert in pending:
        alert.email_sent = True
        alert.emailed_at = now
    Alert.objects.bulk_update(pending, ["email_sent", "emailed_at"])
    logger.info("Alert email sent to %s (%d alert(s))", recipient, len(pending))
    return len(pending)


def run_alert_sweep(
    project_configs: Sequence[object],
    operator_config: OperatorConfig | None,
) -> None:
    """Detect new alerts and send the batched email. Never raises.

    Called once per worker poll cycle, after PR polling and security
    email ingestion, so both alert sources see this cycle's data.
    """
    if operator_config is None or not operator_config.alerts.enabled:
        return
    try:
        new_pr_alerts = sweep_pr_alerts(project_configs, operator_config)
        new_report_alerts = sweep_security_report_alerts(project_configs, operator_config)
        if new_pr_alerts or new_report_alerts:
            logger.info(
                "Alert sweep: %d working-overlap, %d security-report alert(s) raised",
                len(new_pr_alerts),
                len(new_report_alerts),
            )
        send_pending_alert_emails(operator_config)
    except Exception:
        logger.exception("Alert sweep failed")
