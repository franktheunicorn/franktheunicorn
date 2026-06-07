"""Optional RLM rejection judge (v1.5, opt-in, additive).

A *second opinion* on a generated finding: the RLM independently re-reviews
the finding's own hunk and we infer a P(rejection) from whether the
re-review corroborates the finding at the same location. This never replaces
the sklearn :class:`RejectionPredictor`; the drafter combines the two values
per ``rejection_combine``. Gated behind
``ProjectConfig.rlm_scoring.rejection_judge_enabled``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import RLMScoringConfig
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.review.backends.base import PRContext, ReviewFinding

logger = logging.getLogger(__name__)

# Findings within this many lines count as corroborating the same concern.
_CORROBORATION_PROXIMITY = 5

# P(rejection) outcomes for the three corroboration cases.
_P_CORROBORATED = 0.15  # re-review flags the same spot → finding is solid
_P_OTHER_FINDINGS = 0.55  # re-review flags elsewhere → original is shakier
_P_NO_FINDINGS = 0.70  # re-review finds nothing → likely low-value noise


def combine_rejection(sklearn_p: float | None, rlm_p: float, mode: str) -> float:
    """Combine the sklearn and RLM rejection probabilities per ``mode``."""
    if sklearn_p is None or mode == "rlm-only":
        return rlm_p
    if mode == "average":
        return (sklearn_p + rlm_p) / 2
    # Default / "max": be conservative (suppress only when both lean reject low,
    # surface when either is confident the operator would keep it).
    return max(sklearn_p, rlm_p)


def _build_context(pr: PullRequest, governance: str) -> PRContext:
    from franktheunicorn.review.backends.base import PRContext

    return PRContext(
        pr_title=pr.title,
        pr_body=pr.body or "",
        pr_author=pr.author,
        pr_number=pr.number,
        project_name=getattr(getattr(pr, "project", None), "full_name", ""),
        review_context="",
        review_style="",
        tone="",
        test_expectations="",
        governance=governance,
        project_id=getattr(pr, "project_id", None),
        pr_id=getattr(pr, "pk", None),
    )


def _corroborates(finding: ReviewFinding, second_opinion: list[ReviewFinding]) -> bool:
    """True if any re-review finding targets the same file and nearby line."""
    target_line = finding.line_number or 0
    for other in second_opinion:
        if other.file_path != finding.file_path:
            continue
        if abs((other.line_number or 0) - target_line) <= _CORROBORATION_PROXIMITY:
            return True
    return False


def judge_rejection(
    finding: ReviewFinding,
    code_context: str,
    pr: PullRequest,
    scoring: RLMScoringConfig,
    governance: str,
) -> float:
    """Return an RLM-derived P(rejection) in [0, 1] for ``finding``.

    Best-effort: returns a neutral 0.5 on any failure so the caller's combine
    step degrades gracefully.

    This runs per finding by design — it's an independent second opinion on one
    finding's own hunk. The engine/backend setup is cheap relative to the single
    leaf LLM call it makes, so there's no per-finding batching to gain here.
    """
    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.rlm.engine import RLMEngine
    from franktheunicorn.scoring.rlm_interest import _rlm_config_from_scoring

    pr_context = _build_context(pr, governance)
    diff = code_context or (f"+++ b/{finding.file_path}\n" if finding.file_path else "")
    if not diff:
        return 0.5

    rlm_config = _rlm_config_from_scoring(scoring)
    leaf_config = scoring.leaf
    engine = RLMEngine(rlm_config, lambda: get_backend(leaf_config))
    try:
        result = engine.review(diff, pr_context)
    except Exception:
        logger.debug("RLM rejection judge failed for PR #%s.", pr.number, exc_info=True)
        return 0.5

    if not result.findings:
        return _P_NO_FINDINGS
    if _corroborates(finding, result.findings):
        return _P_CORROBORATED
    return _P_OTHER_FINDINGS
