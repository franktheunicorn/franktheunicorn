"""The cheap first pass: is this PR close to mergeable?

Under ``auto_review_policy: triage`` every PR head gets one call here, and the
full review (every LLM backend, every agent CLI) only runs on the ones it calls
likely mergeable or touching a ``review_areas_of_interest`` entry — plus the
PRs ``interest_reason`` already cares about. The suggestion is kept either way:
it is what the PR needs to land, and that is worth reading even when nothing
else ran.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from franktheunicorn.core.untrusted import UNTRUSTED_CONTENT_RULE, fence, fence_note, fresh_nonce

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)

LIKELY_MERGEABLE = "likely-mergeable"
NEEDS_WORK = "needs-work"
UNCLEAR = "unclear"
_VERDICTS = frozenset({LIKELY_MERGEABLE, NEEDS_WORK, UNCLEAR})
_last_no_verdict_warning = float("-inf")

_SYSTEM = f"""\
You triage pull requests for a busy open-source maintainer. Decide whether this PR
is close to mergeable as it stands: the change is coherent, scoped, tested where it
should be, and nothing obvious blocks it. You are not reviewing line by line; the
maintainer's full review runs afterwards if you say it is close, and that review is
how it gets approved. So no approvals yet and pending CI are normal, not blockers;
failing CI, requested changes, merge conflicts and missing tests are. Say
likely-mergeable only if you expect it to land after minor comments at most; a PR
that needs design discussion needs work, and when unsure, say unclear.

{UNTRUSTED_CONTENT_RULE}

Answer with one JSON object and nothing after it:
{{"verdict": "likely-mergeable" | "needs-work" | "unclear",
  "summary": "<one or two sentences: why>",
  "suggestion": "<what it needs to land, addressed to the maintainer; empty if nothing>",
  "touches_area_of_interest": <true if it touches any listed area of interest>}}
"""


@dataclass
class Triage:
    verdict: str
    summary: str = ""
    suggestion: str = ""
    touches_area: bool = False
    source: str = ""

    @property
    def wants_full_review(self) -> bool:
        return self.verdict == LIKELY_MERGEABLE or self.touches_area


def interest_reason(pr: PullRequest, pc: ProjectConfig, operator_username: str) -> str:
    """Why *pr* gets a full review regardless of triage, or "" if it doesn't."""
    from franktheunicorn.scoring.signals import path_matches
    from franktheunicorn.worker.runner import _should_auto_review

    if operator_username and _should_auto_review(pr, operator_username):
        return "you are involved"
    author = (pr.author or "").lower()
    if author in {a.lower() for a in [*pc.frequent_contributors, *pc.committers]}:
        return f"{pr.author} is a contributor you follow"
    if any(path_matches(f, wp) for f in pr.changed_files or [] for wp in pc.watched_paths):
        return "it touches watched_paths"
    if pr.merge_intent:
        return "it is already approved"
    if pr.merge_readiness_verdict:
        return "it is almost ready to merge"
    return ""


def _state(pr: PullRequest) -> list[object]:
    """What the verdict read besides the code that moves without a push: red CI,
    conflicts, reviews. Not pending CI or an uncomputed mergeable; those churn."""
    reviews = sorted((login, r.get("state")) for login, r in (pr.review_states or {}).items())
    return [pr.ci_status in ("failure", "error"), pr.mergeable is False, [list(r) for r in reviews]]


def current_triage(pr: PullRequest) -> Triage | None:
    """The stored triage, if it was for *pr*'s current head and state."""
    if not pr.mergeability_sha or pr.mergeability_sha != (pr.head_sha or ""):
        return None
    detail = pr.mergeability_detail or {}
    if detail.get("state") != _state(pr):
        return None
    return Triage(
        verdict=pr.mergeability_verdict,
        summary=str(detail.get("summary", "")),
        suggestion=str(detail.get("suggestion", "")),
        touches_area=bool(detail.get("touches_area")),
        source=str(detail.get("source", "")),
    )


def build_prompt(pr: PullRequest, pc: ProjectConfig, diff: str, max_diff_chars: int) -> str:
    nonce = fresh_nonce()
    approvals = sorted(
        login for login, r in (pr.review_states or {}).items() if r.get("state") == "APPROVED"
    )
    changes = sorted(
        login
        for login, r in (pr.review_states or {}).items()
        if r.get("state") == "CHANGES_REQUESTED"
    )
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + f"\n[... diff truncated at {max_diff_chars} chars]"
    areas = "\n".join(f"- {a}" for a in pc.review_areas_of_interest) or "(none listed)"
    facts = [
        f"Project: {pc.owner}/{pc.repo}",
        f"Target branch: {pr.base_branch or 'unknown'}",
        f"Author: {pr.author}",
        f"Size: +{pr.additions}/-{pr.deletions} in {len(pr.changed_files or [])} file(s)",
        f"Mergeable (no conflicts): {'unknown' if pr.mergeable is None else pr.mergeable}",
        f"CI: {pr.ci_status or 'unknown'}",
        f"Approved by: {', '.join(approvals) or 'nobody'}",
        f"Changes requested by: {', '.join(changes) or 'nobody'}",
    ]
    return "\n".join(
        [
            *facts,
            "",
            "Areas of interest for this project:",
            areas,
            "",
            fence_note(nonce),
            fence("title", pr.title or "", nonce),
            fence("description", pr.body or "", nonce),
            fence("diff", diff, nonce),
        ]
    )


def parse_triage(raw: str) -> Triage | None:
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
        return Triage(
            verdict=verdict,
            summary=str(data.get("summary") or "")[:1000],
            suggestion=str(data.get("suggestion") or "")[:2000],
            touches_area=data.get("touches_area_of_interest") is True,
        )
    return None


def triage_backend_configs(operator_config: OperatorConfig) -> list[LLMBackendConfig]:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.review.agent_cli import usage_paused

    by_name = {rc.name: rc for rc in operator_config.agent_cli_reviewers}
    configs: list[LLMBackendConfig] = []
    for name in operator_config.mergeability_triage.backends:
        if name == "llm":
            configs.extend(operator_config.llm_backends)
        elif name in by_name and not usage_paused(by_name[name]):
            configs.append(LLMBackendConfig(provider="agent-cli", reviewer=name))
    return configs


def run_triage(
    pr: PullRequest, pc: ProjectConfig, operator_config: OperatorConfig, diff: str
) -> Triage | None:
    """Ask each configured backend in turn; the first parseable answer wins."""
    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.backends.base import BaseLLMBackend

    prompt = build_prompt(pr, pc, diff, operator_config.mergeability_triage.max_diff_chars)
    for cfg in triage_backend_configs(operator_config):
        label = f"{cfg.provider}/{cfg.reviewer or cfg.model or 'default'}"
        try:
            backend = get_backend(cfg)
            if not isinstance(backend, BaseLLMBackend):
                continue
            raw = backend.metered_call(
                _SYSTEM,
                prompt,
                action_type="mergeability_triage",
                project_id=pr.project_id,
                pr_id=pr.pk,
            )
        except Exception:
            logger.debug("Mergeability triage via %s failed", label, exc_info=True)
            continue
        triage = parse_triage(raw)
        if triage is not None:
            triage.source = label
            return triage
        logger.debug("Mergeability triage via %s gave no verdict: %r", label, (raw or "")[:200])
    global _last_no_verdict_warning
    # Every backend down is one cause behind hundreds of PRs a cycle; say it every 10 min.
    if time.monotonic() - _last_no_verdict_warning > 600:
        _last_no_verdict_warning = time.monotonic()
        logger.warning(
            "Mergeability triage got no verdict (PR #%d) from any of mergeability_triage."
            "backends %s; such PRs get no full review and are retried next cycle.",
            pr.number,
            operator_config.mergeability_triage.backends,
        )
    return None


def record_triage(pr: PullRequest, triage: Triage) -> None:
    from franktheunicorn.core.models import PullRequest as PullRequestModel

    pr.mergeability_verdict = triage.verdict
    pr.mergeability_sha = pr.head_sha or ""
    pr.mergeability_detail = {
        "summary": triage.summary,
        "suggestion": triage.suggestion,
        "touches_area": triage.touches_area,
        "source": triage.source,
        "state": _state(pr),
    }
    PullRequestModel.objects.filter(pk=pr.pk).update(
        mergeability_verdict=pr.mergeability_verdict,
        mergeability_sha=pr.mergeability_sha,
        mergeability_detail=pr.mergeability_detail,
    )
