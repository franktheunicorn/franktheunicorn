"""Severity calibration for sub-check findings — combat "everything is critical".

Mantis's ``/mantis-calibrate`` stage gives each finding a numerical risk
score (1-10) and surfaces the most critical rather than spewing thousands of
"criticals". franktheunicorn's findings are categorical (critical / important
/ nit / informational) with no cap, so a sub-check that flags eight "critical"
findings surfaces all eight.

This scores each finding numerically from its severity and confidence, then
caps to the top-N per sub-check — applied as a post-check filter in
``run_enabled_checks`` before draft creation. The main review pipeline
(``drafter``) is not calibrated here; that's a follow-up.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import ReviewFinding

if TYPE_CHECKING:
    from franktheunicorn.config.models import CalibrationConfig

logger = logging.getLogger(__name__)

#: Base score by severity label. Mantis's calibrator weights impact; the
#: severity the agent assigned is the closest proxy we have without a second
#: LLM pass.
_SEVERITY_SCORE: dict[str, float] = {
    "critical": 9.0,
    "important": 7.0,
    "high": 7.0,
    "medium": 5.0,
    "nit": 3.0,
    "informational": 1.0,
    "low": 2.0,
    "unknown": 5.0,
}

#: Terms that signal a finding names a concrete trust boundary or
#: attacker-controlled path — the "name the boundary" rule. Trimmed to
#: unambiguous security terms; common words (input, trust, route, endpoint)
#: are noise in general review and were removed.
_EVIDENCE_KEYWORDS = (
    "boundary",
    "attacker",
    "untrusted",
    "user-controlled",
    "sink",
)

#: Title hedges that signal the agent isn't sure there's a real issue.
#: Trimmed to claim-level hedging — "could"/"consider"/"might" are normal
#: review diction (tone guard's territory), not calibration signals.
_VAGUE_WORDS = ("potential", "possible", "maybe")


def calibrate_findings(
    findings: list[ReviewFinding],
    config: CalibrationConfig,
) -> list[ReviewFinding]:
    """Score findings 1-10 and cap to the top-N per check.

    Returns the surfaced subset, highest score first. Findings below
    ``min_score`` are dropped; the rest are capped at ``max_findings``. A
    disabled config is a pass-through (the operator opted out of calibration).
    """
    if not config.enabled:
        return findings

    scored = [(f, _score(f)) for f in findings]
    kept = [(f, s) for f, s in scored if s >= config.min_score]
    kept.sort(key=lambda fs: fs[1], reverse=True)
    cap = config.max_findings
    surfaced = [f for f, _s in kept] if cap == 0 else [f for f, _s in kept[:cap]]

    if len(findings) > len(surfaced):
        logger.info(
            "Calibration surfaced %d of %d finding(s) (min_score=%.1f, max=%d).",
            len(surfaced),
            len(findings),
            config.min_score,
            config.max_findings,
        )
    return surfaced


def _score(finding: ReviewFinding) -> float:
    """Numerical risk score 1-10 from severity, confidence, and evidence."""
    base = _SEVERITY_SCORE.get(finding.severity.lower().strip(), 5.0)
    # Confidence scales the base: a high-confidence finding keeps its score,
    # a low-confidence one drops toward half. (In the prompt path confidence is
    # a function of severity, so this term is near-constant there; it varies
    # for hand-built sub-check findings.)
    score = base * (0.5 + 0.5 * finding.confidence)
    body = finding.body.lower()
    if any(kw in body for kw in _EVIDENCE_KEYWORDS):
        score += 1.0
    if any(w in finding.title.lower() for w in _VAGUE_WORDS):
        score -= 2.0
    if len(finding.body) < 50:
        score -= 1.0
    return max(1.0, min(10.0, round(score, 1)))
