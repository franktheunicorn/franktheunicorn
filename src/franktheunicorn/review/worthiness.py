"""The cheap "is this PR worth a full review?" screen.

Under a project's ``minimize_reviews: true`` flag, a PR that passes the
``auto_review_policy`` gate still has to earn its full review. The
deterministic signals (``interest_reason`` — involvement, followed
contributors, watched paths — plus ``looks_close_to_mergeable``) are checked
first and cost nothing. Whatever is left gets one cheap model call here: a
Y/N on whether a full review is worth the spend, cached per head + CI/review
state so a PR is screened once, not once a cycle.

The screen only ever *skips* reviews. A PR it declines stays ingested,
scored, and dashboard-visible, and the dashboard's Force Run Agents button
reviews it on demand.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from franktheunicorn.core.untrusted import UNTRUSTED_CONTENT_RULE, fence, fence_note, fresh_nonce

if TYPE_CHECKING:
    from franktheunicorn.config.models import (
        LLMBackendConfig,
        OperatorConfig,
        ProjectConfig,
        ReviewWorthinessConfig,
    )
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)

WORTH_REVIEWING = "worth-reviewing"
NOT_WORTH_REVIEWING = "not-worth-reviewing"
UNCLEAR = "unclear"
_VERDICTS = frozenset({WORTH_REVIEWING, NOT_WORTH_REVIEWING, UNCLEAR})
_last_no_verdict_warning = float("-inf")

_SYSTEM = f"""\
You screen pull requests for a busy open-source maintainer whose AI review
pipeline costs real money per run. The cheap deterministic filters already ran
and did NOT catch this PR: the maintainer is not involved, the author is not
one they follow, it touches none of their watched paths, and it is not close
to merging. Your only question: would a careful reviewer still find something
here worth a comment — a correctness risk, a security problem, a behavior or
public-API change with release consequences, a missing test on a risky path,
or project-specific red flags? Routine chores (typos, version bumps, comment
polish, mechanical refactors) are not worth a full review. When genuinely
unsure, say unclear; leaning no is right for chores.

{UNTRUSTED_CONTENT_RULE}

Answer with one JSON object and nothing after it:
{{"verdict": "worth-reviewing" | "not-worth-reviewing" | "unclear",
  "reason": "<one sentence: why>"}}
"""


@dataclass
class Worthiness:
    verdict: str
    reason: str = ""
    source: str = ""

    @property
    def worth_reviewing(self) -> bool:
        """The screen exists to cut volume: only a firm yes spends the review."""
        return self.verdict == WORTH_REVIEWING


def looks_close_to_mergeable(pr: PullRequest) -> str:
    """Deterministic closeness signals from the stored row, or "".

    Read by the minimize_reviews gate before any model call: a PR that is
    already approved or almost ready is exactly the one a full review should
    not skip — the LGTM decision is the point.
    """
    from franktheunicorn.review.mergeability import LIKELY_MERGEABLE, current_triage
    from franktheunicorn.scoring.merge_readiness import LISTED_VERDICTS

    if pr.merge_intent:
        return "already approved (merge intent)"
    if pr.merge_readiness_verdict in LISTED_VERDICTS:
        return "almost ready to merge"
    if (pr.approval_count or 0) > 0:
        return "has approvals"
    triage = current_triage(pr)
    if triage is not None and triage.verdict == LIKELY_MERGEABLE:
        return "triage said likely-mergeable"
    return ""


def current_worthiness(pr: PullRequest) -> Worthiness | None:
    """The stored screen verdict, if it was for *pr*'s current head and state."""
    from franktheunicorn.review.mergeability import _state

    if not pr.worthiness_sha or pr.worthiness_sha != (pr.head_sha or ""):
        return None
    detail = pr.worthiness_detail or {}
    if detail.get("state") != _state(pr):
        return None
    return Worthiness(
        verdict=pr.worthiness_verdict,
        reason=str(detail.get("reason", "")),
        source=str(detail.get("source", "")),
    )


def build_prompt(pr: PullRequest, pc: ProjectConfig, diff: str, max_diff_chars: int) -> str:
    nonce = fresh_nonce()
    approvals = sorted(
        login for login, r in (pr.review_states or {}).items() if r.get("state") == "APPROVED"
    )
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + f"\n[... diff truncated at {max_diff_chars} chars]"
    areas = "\n".join(f"- {a}" for a in pc.review_areas_of_interest) or "(none listed)"
    facts = [
        f"Project: {pc.owner}/{pc.repo}",
        f"Author: {pr.author}",
        f"Size: +{pr.additions}/-{pr.deletions} in {len(pr.changed_files or [])} file(s)",
        f"Mergeable (no conflicts): {'unknown' if pr.mergeable is None else pr.mergeable}",
        f"CI: {pr.ci_status or 'unknown'}",
        f"Approved by: {', '.join(approvals) or 'nobody'}",
    ]
    return "\n".join(
        [
            *facts,
            "",
            "Project-specific areas that always earn a review:",
            areas,
            "",
            fence_note(nonce),
            fence("title", pr.title or "", nonce),
            fence("description", pr.body or "", nonce),
            fence("diff", diff, nonce),
        ]
    )


def parse_worthiness(raw: str) -> Worthiness | None:
    from franktheunicorn.review.backends.base import json_object_candidates

    for candidate in json_object_candidates(raw or ""):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict not in _VERDICTS:
            continue
        return Worthiness(verdict=verdict, reason=str(data.get("reason") or "")[:1000])
    return None


def worthiness_backend_configs(operator_config: OperatorConfig) -> list[LLMBackendConfig]:
    """The explicit cheap backend, else every llm_backends entry in order."""
    cfg: ReviewWorthinessConfig = operator_config.review_worthiness
    if cfg.backend is not None:
        return [cfg.backend]
    return list(operator_config.llm_backends)


def run_worthiness_screen(
    pr: PullRequest, pc: ProjectConfig, operator_config: OperatorConfig, diff: str
) -> Worthiness | None:
    """Ask each configured backend in turn; the first parseable answer wins."""
    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.backends.base import BaseLLMBackend

    prompt = build_prompt(pr, pc, diff, operator_config.review_worthiness.max_diff_chars)
    for cfg in worthiness_backend_configs(operator_config):
        label = f"{cfg.provider}/{cfg.model or 'default'}"
        try:
            backend = get_backend(cfg)
            if not isinstance(backend, BaseLLMBackend):
                continue
            raw = backend.metered_call(
                _SYSTEM,
                prompt,
                action_type="worthiness_screen",
                project_id=pr.project_id,
                pr_id=pr.pk,
            )
        except Exception:
            logger.debug("Worthiness screen via %s failed", label, exc_info=True)
            continue
        worthiness = parse_worthiness(raw)
        if worthiness is not None:
            worthiness.source = label
            return worthiness
        logger.debug("Worthiness screen via %s gave no verdict: %r", label, (raw or "")[:200])
    global _last_no_verdict_warning
    # Every backend down is one cause behind hundreds of PRs a cycle; say it every 10 min.
    if time.monotonic() - _last_no_verdict_warning > 600:
        _last_no_verdict_warning = time.monotonic()
        logger.warning(
            "Worthiness screen got no verdict (PR #%d) from any configured backend; "
            "such PRs get no full review and are retried next cycle.",
            pr.number,
        )
    return None


def record_worthiness(pr: PullRequest, worthiness: Worthiness) -> None:
    from franktheunicorn.core.models import PullRequest as PullRequestModel
    from franktheunicorn.review.mergeability import _state

    pr.worthiness_verdict = worthiness.verdict
    pr.worthiness_sha = pr.head_sha or ""
    pr.worthiness_detail = {
        "reason": worthiness.reason,
        "source": worthiness.source,
        "state": _state(pr),
    }
    PullRequestModel.objects.filter(pk=pr.pk).update(
        worthiness_verdict=pr.worthiness_verdict,
        worthiness_sha=pr.worthiness_sha,
        worthiness_detail=pr.worthiness_detail,
    )
