"""Optional RLM-based interest judge (v1.5, opt-in).

Produces the ``llm_interest`` scoring signal — which nothing populates by
default — by recursively reviewing the PR and mapping the aggregated findings
to a ``"high"``/``"medium"`` label. Gated behind
``ProjectConfig.rlm_scoring.interest_enabled``; when disabled the caller never
invokes this and the signal is skipped entirely.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig, RLMConfig, RLMScoringConfig
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.review.backends.base import PRContext

logger = logging.getLogger(__name__)


def build_minimal_context(pr: PullRequest, project_config: ProjectConfig) -> PRContext:
    """Build a DB-light ``PRContext`` for RLM judging (no anti-pattern queries)."""
    from franktheunicorn.review.backends.base import PRContext

    project_name = getattr(getattr(pr, "project", None), "full_name", "") or (
        f"{project_config.owner}/{project_config.repo}"
    )
    return PRContext(
        pr_title=pr.title,
        pr_body=pr.body or "",
        pr_author=pr.author,
        pr_number=pr.number,
        project_name=project_name,
        review_context=project_config.review_context,
        review_style="",
        tone=project_config.tone,
        test_expectations=project_config.test_expectations,
        governance=project_config.governance,
        project_id=getattr(pr, "project_id", None),
        pr_id=getattr(pr, "pk", None),
    )


def _rlm_config_from_scoring(scoring: RLMScoringConfig) -> RLMConfig:
    from franktheunicorn.config.models import RLMConfig

    return RLMConfig(
        leaf=scoring.leaf,
        max_sub_calls=scoring.max_sub_calls,
        leaf_token_budget=scoring.leaf_token_budget,
        total_token_budget=scoring.total_token_budget,
    )


def judge_interest(
    pr: PullRequest,
    project_config: ProjectConfig,
    *,
    diff: str = "",
) -> str | None:
    """Return ``"high"``/``"medium"`` interest, or ``None`` to skip the signal.

    Best-effort: any failure returns ``None`` so scoring degrades gracefully.
    Works from the provided ``diff`` (or a changed-files placeholder when none
    is supplied).
    """
    scoring = project_config.rlm_scoring
    if not scoring.interest_enabled:
        return None

    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.rlm.aggregate import interest_label_from_findings
    from franktheunicorn.review.rlm.engine import RLMEngine

    pr_context = build_minimal_context(pr, project_config)
    if not diff:
        files: list[str] = getattr(pr, "changed_files", None) or []
        diff = "\n".join(f"+++ b/{f}" for f in files) + "\n" if files else ""
    if not diff:
        return None

    rlm_config = _rlm_config_from_scoring(scoring)
    leaf_config = scoring.leaf
    engine = RLMEngine(rlm_config, lambda: get_backend(leaf_config))
    try:
        result = engine.review(diff, pr_context)
    except Exception:
        logger.debug("RLM interest judge failed for PR #%s.", pr.number, exc_info=True)
        return None

    return interest_label_from_findings(result.findings)
