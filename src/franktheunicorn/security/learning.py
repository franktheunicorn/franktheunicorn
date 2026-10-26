"""Iterative learning loop for security triage.

Mirrors the anti-pattern learning loop (see ``review/antipattern.py``), but
for security triage verdicts: the operator agrees or disagrees with an LLM
triage verdict, that feedback is recorded, and periodically distilled by an
LLM into a short "learned guidance" addendum that gets injected into future
triage system prompts (see ``security/prompt.py::build_triage_prompt`` and
``security/triage.py::_analyze_report``).
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Count, Q
from django.utils import timezone

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

#: Same bound for the operator's own rulings, the loop's second input.
_MAX_RULINGS_FOR_DISTILLATION = 50

#: The statuses that mean "a person ruled on this", as opposed to staged machine
#: output — ``status`` is the operator's field (triage stages its suggestion in
#: ``auto_triage_status``), so a non-auto status is unambiguously a ruling.
RULED_STATUSES = (
    "valid",
    "invalid",
    "duplicate",
    "expected-behavior",
    "defense-in-depth",
    "info-only",
)

_DISTILL_SYSTEM_PROMPT = (
    "You are distilling an operator's judgment on security triage into concise, "
    "general guidance for future triage. You are given two kinds of evidence: "
    "their agree/disagree feedback on machine triage verdicts, and a sample of "
    "their own rulings on reports (the status they chose, the severity, and "
    "their notes).\n\n"
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
    *,
    distill: bool = True,
) -> SecurityTriageFeedback:
    """Record operator agree/disagree feedback on a triage verdict.

    Snapshots the report's current verdict text so the feedback row remains
    meaningful even if the report is later re-triaged or deleted. Then makes
    a best-effort attempt to distill accumulated feedback into updated
    guidance — a distillation failure never prevents the feedback from being
    saved.

    ``distill=False`` is for the implicit captures (a verdict save that matched
    or overrode the staged suggestion, the Agree button): those record the
    signal without spending a model call per click. The dashboard's guidance
    page has the button that distills them.
    """
    feedback = SecurityTriageFeedback.objects.create(
        report=report,
        project=report.project,
        agreed=agreed,
        operator_comment=operator_comment,
        triage_summary_snapshot=report.triage_summary,
        assessed_severity_snapshot=report.assessed_severity,
    )

    if distill:
        try:
            distill_triage_guidance(report.project, operator_config)
        except Exception:
            logger.exception("Failed to distill triage guidance after feedback %d", feedback.pk)

    return feedback


def distill_triage_guidance(
    project: Project | None,
    operator_config: OperatorConfig,
) -> SecurityTriageGuidance | None:
    """Summarize what the operator has taught the triage loop into learned guidance.

    Two inputs, because agreement alone is the thinner half of the signal:
    agree/disagree feedback rows (scoped to ``project`` plus global rows), and
    the operator's *own rulings* — reports they ruled valid/invalid/duplicate/
    expected-behavior, with the severity and notes they chose. The second is how
    the loop learns from triage the operator did entirely by hand, which is most
    of it on a fresh backlog. For ``project=None`` (the global addendum) rulings
    from every project are fair game.

    Upserts the active :class:`SecurityTriageGuidance` row for ``project``.
    Degrades gracefully (returns ``None``) when there is nothing to learn from,
    no LLM backend configured, or the call fails.
    """
    feedback_qs = SecurityTriageFeedback.objects.filter(
        Q(project=project) | Q(project__isnull=True)
    ).order_by("-created_at")[:_MAX_FEEDBACK_FOR_DISTILLATION]
    feedback = list(feedback_qs)
    rulings = _operator_rulings(project)
    if not feedback and not rulings:
        return None

    if not operator_config.llm_backends:
        return None

    from franktheunicorn.review.backends import get_backend

    backend = get_backend(operator_config.llm_backends[0])

    user_message = _render_feedback_for_distillation(feedback)
    if rulings:
        user_message += "\n\n" + _render_rulings_for_distillation(rulings)

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
            "source_feedback_count": len(feedback) + len(rulings),
        },
    )
    return guidance


def _operator_rulings(project: Project | None) -> list[SecurityReport]:
    """Recent reports the operator ruled on themselves — the loop's second input.

    A ruling is a non-auto ``status`` (see :data:`RULED_STATUSES`); whether it
    came from the verdict form, the Agree button, or a sheet import doesn't
    matter — each is a decision the operator owns. ``project=None`` (the global
    addendum) learns from every project's rulings.
    """
    qs = SecurityReport.objects.filter(status__in=RULED_STATUSES)
    if project is not None:
        qs = qs.filter(project=project)
    return list(qs.order_by("-updated_at")[:_MAX_RULINGS_FOR_DISTILLATION])


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


def _render_rulings_for_distillation(rulings: list[SecurityReport]) -> str:
    """Render the operator's own rulings for the distillation call.

    Bounded per entry: the distillation wants the shape of the operator's
    judgment, not 50 full reports, and a note is capped so one pasted stack
    trace can't crowd the other rulings out of the prompt.
    """
    lines = ["## The Operator's Own Rulings (newest first)\n"]
    for report in rulings:
        title = (report.title or report.raw_text[:120]).replace("\n", " ")[:160]
        lines.append(f"- Ruled **{report.status}** (severity: {report.assessed_severity}): {title}")
        if report.operator_notes:
            lines.append(f"  - Notes: {report.operator_notes[:400]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Agreement export — how often the operator agreed with the machine's verdict
# --------------------------------------------------------------------------- #

#: Label for feedback rows whose project is gone or was never set. A
#: :class:`SecurityTriageFeedback` keeps its own ``project`` FK (nullable, and
#: the report FK is ``SET_NULL``), so a row can outlive the project it was about;
#: it still counts toward the overall rate and gets its own line.
_NO_PROJECT_LABEL = "(no project)"

#: The label of the roll-up line. Reserved so a project can't collide with it:
#: ``Project.full_name`` is always ``owner/repo`` and so always contains a slash.
_OVERALL_LABEL = "ALL PROJECTS"


@dataclass(frozen=True)
class TriageAgreementRow:
    """One line of the agreement export.

    A project (or the overall roll-up) with how many triage verdicts the
    operator agreed vs. disagreed with. The denominator is feedback *events*,
    not reports: every agree/disagree — the feedback widget, the Agree button,
    and a verdict save that matched or overrode the staged suggestion — is one
    :class:`SecurityTriageFeedback` row (see
    ``dashboard.views.security_report_set_verdict``), so a report re-ruled twice
    contributes twice, which is the honest denominator for "how often do we
    agree when I rule".
    """

    label: str
    agreed: int
    disagreed: int

    @property
    def total(self) -> int:
        return self.agreed + self.disagreed

    @property
    def agree_pct(self) -> float | None:
        """Agreement rate, or ``None`` when there's nothing to divide by."""
        return 100.0 * self.agreed / self.total if self.total else None

    @property
    def disagree_pct(self) -> float | None:
        return 100.0 * self.disagreed / self.total if self.total else None


def triage_agreement_rows() -> list[TriageAgreementRow]:
    """Per-project agree/disagree tallies plus an overall roll-up.

    Pure read. One grouped query over every :class:`SecurityTriageFeedback` row,
    bounded by the number of projects, so it never streams: the result is one
    line per project (ordered ``owner/repo``), rows with no project folded into
    :data:`_NO_PROJECT_LABEL`, and a final :data:`_OVERALL_LABEL` line summing
    them. An install with no feedback yet returns a single zero overall line, so
    the export is always a valid, self-explaining CSV rather than a bare header.
    """
    # The annotation aliases are deliberately *not* ``agreed``/``disagreed``: the
    # model field is ``agreed``, and an alias of the same name makes the second
    # filter's ``Q(agreed=False)`` resolve to the first annotation instead of the
    # column, which SQLite rejects as a nested aggregate ("misuse of COUNT()").
    grouped = (
        SecurityTriageFeedback.objects.values("project__owner", "project__repo")
        .annotate(
            n_agreed=Count("pk", filter=Q(agreed=True)),
            n_disagreed=Count("pk", filter=Q(agreed=False)),
        )
        .order_by("project__owner", "project__repo")
    )

    rows: list[TriageAgreementRow] = []
    total_agreed = 0
    total_disagreed = 0
    for entry in grouped:
        owner = entry["project__owner"]
        repo = entry["project__repo"]
        label = f"{owner}/{repo}" if owner else _NO_PROJECT_LABEL
        rows.append(TriageAgreementRow(label, entry["n_agreed"], entry["n_disagreed"]))
        total_agreed += entry["n_agreed"]
        total_disagreed += entry["n_disagreed"]

    rows.append(TriageAgreementRow(_OVERALL_LABEL, total_agreed, total_disagreed))
    return rows


_AGREEMENT_HEADER = ["project", "agreed", "disagreed", "total", "agree_pct", "disagree_pct"]


def agreement_csv() -> str:
    """Render :func:`triage_agreement_rows` as a CSV string.

    Small and bounded (one line per project), so it's built whole rather than
    streamed like the report export. ``agree_pct``/``disagree_pct`` are blank
    rather than ``0`` for a row with no feedback, so an empty tally reads as "no
    data" instead of "we never agree".
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_AGREEMENT_HEADER)
    for row in triage_agreement_rows():
        writer.writerow(
            [
                row.label,
                row.agreed,
                row.disagreed,
                row.total,
                "" if row.agree_pct is None else f"{row.agree_pct:.1f}",
                "" if row.disagree_pct is None else f"{row.disagree_pct:.1f}",
            ]
        )
    return buffer.getvalue()


def agreement_export_filename() -> str:
    """Dated filename for the agreement export, matching the report export's shape."""
    return f"security-triage-agreement-{timezone.now().date().isoformat()}.csv"
