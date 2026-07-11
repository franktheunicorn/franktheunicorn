"""
Tone Guard — post-generation rewrite pass for constructiveness (§4).

Rewrites draft findings for constructive tone without suppressing them.
Preserves directness and technical precision while removing abrasiveness.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import ReviewFinding

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.review.backends.base import PRContext

logger = logging.getLogger(__name__)

_TONE_GUARD_SYSTEM = """\
You are a tone editor for code review comments. Your job is to rewrite
the comment for constructive tone WITHOUT changing the technical content.

Preserve: directness, technical precision, actionable suggestions.
Remove: unnecessary abrasiveness, pedantic corrections, snarky phrasing,
        condescension, character voice, persona references, whimsy.

{tone_objective}
{personality_guidance}
{addendum}

Return ONLY the rewritten comment text. Do not add preamble or explanation.
If the comment is already fine, return it unchanged."""


def _build_tone_prompt(
    pr_context: PRContext,
    is_new_contributor: bool = False,
    new_contributor_addendum: str = "",
) -> str:
    """Build the tone guard system prompt from project context."""
    tone_objective = f"Tone objective: {pr_context.tone}" if pr_context.tone else ""
    personality_guidance = ""
    if pr_context.personality_external_voice:
        personality_guidance = f"External voice guidance: {pr_context.personality_external_voice}"
    addendum = ""
    if is_new_contributor and new_contributor_addendum:
        addendum = f"NEW CONTRIBUTOR — additional guidance: {new_contributor_addendum}"
    return _TONE_GUARD_SYSTEM.format(
        tone_objective=tone_objective,
        personality_guidance=personality_guidance,
        addendum=addendum,
    )


def apply_tone_guard(
    finding: ReviewFinding,
    pr_context: PRContext,
    backend_config: LLMBackendConfig | None = None,
    is_new_contributor: bool = False,
    new_contributor_addendum: str = "",
    *,
    project_id: int | None = None,
    pr_id: int | None = None,
) -> tuple[ReviewFinding, bool]:
    """Rewrite a finding's body for constructive tone.

    Uses the configured LLM backend to rewrite. Falls back to returning the
    original finding unchanged if no backend is available or the call fails.

    Returns ``(finding, rewritten)`` where ``rewritten`` is True only when
    the rewrite actually succeeded. Callers must not mark drafts
    ``tone_guard_applied`` on the fallback path — the auto-poster's Gate 3
    relies on that flag meaning the rewrite really ran.

    The original body is preserved in the returned finding's ``title`` field
    (used as reasoning trace when persisted).
    """
    if backend_config is None:
        return finding, False

    from franktheunicorn.review.backends import get_backend

    backend = get_backend(backend_config)
    if not hasattr(backend, "metered_call"):
        return finding, False

    system_prompt = _build_tone_prompt(
        pr_context,
        is_new_contributor=is_new_contributor,
        new_contributor_addendum=new_contributor_addendum,
    )

    try:
        rewritten = backend.metered_call(
            system_prompt,
            finding.body,
            action_type="tone-guard",
            project_id=project_id,
            pr_id=pr_id,
        )
    except Exception:
        logger.warning("Tone guard LLM call failed; returning original finding.", exc_info=True)
        return finding, False

    rewritten = rewritten.strip()
    if not rewritten:
        return finding, False

    return ReviewFinding(
        file_path=finding.file_path,
        line_number=finding.line_number,
        title=finding.body,  # preserve original as reasoning trace
        body=rewritten,
        suggestion=finding.suggestion,
        confidence=finding.confidence,
        severity=finding.severity,
    ), True


def apply_tone_guard_batch(
    findings: list[ReviewFinding],
    pr_context: PRContext,
    backend_config: LLMBackendConfig | None = None,
    is_new_contributor: bool = False,
    new_contributor_addendum: str = "",
    *,
    project_id: int | None = None,
    pr_id: int | None = None,
) -> tuple[list[ReviewFinding], list[bool]]:
    """Apply tone guard to a batch of findings.

    Returns the (possibly rewritten) findings plus a parallel list of flags
    marking which findings were actually rewritten.
    """
    if backend_config is None:
        return findings, [False] * len(findings)

    results: list[ReviewFinding] = []
    flags: list[bool] = []
    for f in findings:
        rewritten, ok = apply_tone_guard(
            f,
            pr_context,
            backend_config,
            is_new_contributor=is_new_contributor,
            new_contributor_addendum=new_contributor_addendum,
            project_id=project_id,
            pr_id=pr_id,
        )
        results.append(rewritten)
        flags.append(ok)
    return results, flags
