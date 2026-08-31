"""Iterative learning loop for security triage.

Mirrors the anti-pattern learning loop (see ``review/antipattern.py``), but
for security triage verdicts: the operator agrees or disagrees with an LLM
triage verdict, that feedback is recorded, and periodically distilled by an
LLM into a short "learned guidance" addendum that gets injected into future
triage system prompts (see ``security/prompt.py::build_triage_prompt`` and
``security/triage.py::_analyze_report``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import Q

from franktheunicorn.core.models import (
    Project,
    SecurityReport,
    SecurityTriageFeedback,
    SecurityTriageGuidance,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig

logger = logging.getLogger(__name__)

# Cap on how much feedback history is fed into a single distillation call.
_MAX_FEEDBACK_FOR_DISTILLATION = 100

_DISTILL_SYSTEM_PROMPT = (
    "You are distilling an operator's agree/disagree feedback on security "
    "triage verdicts into concise, general guidance for future triage.\n\n"
    "Summarize recurring patterns as a short bulleted addendum covering:\n"
    "- What kinds of reports the operator treats as expected/documented "
    "behavior versus real findings.\n"
    "- Severity calibration — where the operator's judgment differs from "
    "the raw verdict.\n"
    "- Recurring false-positive (or false-negative) patterns worth flagging "
    "early.\n\n"
    "Be concise and general — write rules that generalize to future reports, "
    "not a recap of any single one. Return ONLY the bulleted guidance text, "
    "no preamble, no markdown fences."
)


def record_triage_feedback(
    report: SecurityReport,
    agreed: bool,
    operator_comment: str,
    operator_config: OperatorConfig,
) -> SecurityTriageFeedback:
    """Record operator agree/disagree feedback on a triage verdict.

    Snapshots the report's current verdict text so the feedback row remains
    meaningful even if the report is later re-triaged or deleted. Then makes
    a best-effort attempt to distill accumulated feedback into updated
    guidance — a distillation failure never prevents the feedback from being
    saved.
    """
    feedback = SecurityTriageFeedback.objects.create(
        report=report,
        project=report.project,
        agreed=agreed,
        operator_comment=operator_comment,
        triage_summary_snapshot=report.triage_summary,
        assessed_severity_snapshot=report.assessed_severity,
    )

    try:
        distill_triage_guidance(report.project, operator_config)
    except Exception:
        logger.exception("Failed to distill triage guidance after feedback %d", feedback.pk)

    return feedback


def distill_triage_guidance(
    project: Project | None,
    operator_config: OperatorConfig,
) -> SecurityTriageGuidance | None:
    """Summarize accumulated feedback for ``project`` into learned guidance.

    Gathers feedback scoped to ``project`` plus global feedback
    (``project=None``), newest first, and asks the LLM to distill it into a
    short addendum. Upserts the active :class:`SecurityTriageGuidance` row
    for ``project``. Degrades gracefully (returns ``None``) when there is no
    feedback, no LLM backend configured, or the call fails.
    """
    feedback_qs = SecurityTriageFeedback.objects.filter(
        Q(project=project) | Q(project__isnull=True)
    ).order_by("-created_at")[:_MAX_FEEDBACK_FOR_DISTILLATION]
    feedback = list(feedback_qs)
    if not feedback:
        return None

    if not operator_config.llm_backends:
        return None

    from franktheunicorn.review.backends import get_backend

    backend = get_backend(operator_config.llm_backends[0])

    user_message = _render_feedback_for_distillation(feedback)

    try:
        guidance_text = backend.complete(user_message, system=_DISTILL_SYSTEM_PROMPT)
    except Exception:
        logger.exception("Triage guidance distillation call failed for project %s", project)
        return None

    guidance_text = guidance_text.strip()
    if not guidance_text:
        return None

    guidance, _created = SecurityTriageGuidance.objects.update_or_create(
        project=project,
        is_active=True,
        defaults={
            "guidance_text": guidance_text,
            "source_feedback_count": len(feedback),
        },
    )
    return guidance


def resolve_triage_guidance(project: Project | None) -> str:
    """Resolve the learned triage guidance to inject for ``project``.

    Precedence (mirrors ``security.triage.resolve_security_model``):
      1. Active project-specific guidance.
      2. Active global guidance (``project=None``).
      3. Empty string.

    Read-only.
    """
    if project is not None:
        project_guidance = (
            SecurityTriageGuidance.objects.filter(project=project, is_active=True)
            .order_by("-updated_at")
            .first()
        )
        if project_guidance and project_guidance.guidance_text.strip():
            return project_guidance.guidance_text.strip()

    global_guidance = (
        SecurityTriageGuidance.objects.filter(project__isnull=True, is_active=True)
        .order_by("-updated_at")
        .first()
    )
    if global_guidance and global_guidance.guidance_text.strip():
        return global_guidance.guidance_text.strip()

    return ""


def _render_feedback_for_distillation(feedback: list[SecurityTriageFeedback]) -> str:
    """Render feedback rows as the user message for the distillation call."""
    lines = ["## Operator Triage Feedback (newest first)\n"]
    for item in feedback:
        verdict = "AGREED" if item.agreed else "DISAGREED"
        lines.append(f"- **{verdict}** (severity: {item.assessed_severity_snapshot or 'unknown'})")
        if item.triage_summary_snapshot:
            lines.append(f"  - LLM verdict: {item.triage_summary_snapshot}")
        if item.operator_comment:
            lines.append(f"  - Operator comment: {item.operator_comment}")
    return "\n".join(lines)
