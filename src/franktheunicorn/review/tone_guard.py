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
Remove: unnecessary abrasiveness, pedantic corrections, snarky phrasing, condescension.

{tone_objective}
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
    addendum = ""
    if is_new_contributor and new_contributor_addendum:
        addendum = f"NEW CONTRIBUTOR — additional guidance: {new_contributor_addendum}"
    return _TONE_GUARD_SYSTEM.format(tone_objective=tone_objective, addendum=addendum)


def apply_tone_guard(
    finding: ReviewFinding,
    pr_context: PRContext,
    backend_config: LLMBackendConfig | None = None,
    is_new_contributor: bool = False,
    new_contributor_addendum: str = "",
) -> ReviewFinding:
    """Rewrite a finding's body for constructive tone.

    Uses the configured LLM backend to rewrite. Falls back to returning the
    original finding unchanged if no backend is available or the call fails.

    The original body is preserved in the returned finding's ``title`` field
    (used as reasoning trace when persisted).
    """
    if backend_config is None:
        return finding

    from franktheunicorn.review.backends import get_backend

    backend = get_backend(backend_config)
    system_prompt = _build_tone_prompt(
        pr_context,
        is_new_contributor=is_new_contributor,
        new_contributor_addendum=new_contributor_addendum,
    )

    try:
        rewritten = backend._call_api(  # noqa: SLF001
            system_prompt,
            finding.body,
            backend._resolve_api_key(),  # noqa: SLF001
        )
    except Exception:
        logger.debug("Tone guard LLM call failed; returning original finding.", exc_info=True)
        return finding

    rewritten = rewritten.strip()
    if not rewritten:
        return finding

    return ReviewFinding(
        file_path=finding.file_path,
        line_number=finding.line_number,
        title=finding.body,  # preserve original as reasoning trace
        body=rewritten,
        suggestion=finding.suggestion,
        confidence=finding.confidence,
        severity=finding.severity,
    )


def apply_tone_guard_batch(
    findings: list[ReviewFinding],
    pr_context: PRContext,
    backend_config: LLMBackendConfig | None = None,
    is_new_contributor: bool = False,
    new_contributor_addendum: str = "",
) -> list[ReviewFinding]:
    """Apply tone guard to a batch of findings."""
    if backend_config is None:
        return findings

    return [
        apply_tone_guard(
            f,
            pr_context,
            backend_config,
            is_new_contributor=is_new_contributor,
            new_contributor_addendum=new_contributor_addendum,
        )
        for f in findings
    ]
