"""LLM triage of a PR's failed CI checks: flake, the PR's fault, or CI infra.

Runs in the worker (the analyze_ci_failures command), queued when the operator
opens a PR. One CIFailureAnalysis row per failed check run, so a second look
at the same head only pays for checks that failed since.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from franktheunicorn.core.models import CIFailureAnalysis

if TYPE_CHECKING:
    from franktheunicorn.backends.base import ForgeClient
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.review.backends.base import BaseLLMBackend

logger = logging.getLogger(__name__)

_EXCERPT_CHARS = 12_000
_TAIL_LINES = 80
_CONTEXT = 4
_ERROR_LINE = re.compile(
    r"error|fail|exception|traceback|assert|panic|killed|out of memory|timed? ?out|\*\*\* ",
    re.IGNORECASE,
)
# Actions prefixes every log line with an ISO timestamp.
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")

_SYSTEM = """You triage CI failures on open-source pull requests.

Given one failed check's log excerpt and what the PR changes, decide which it most likely is:
- "flake": intermittent or unrelated to this change (timing, a known-flaky test, a test the PR
  doesn't touch failing in a way its diff can't explain)
- "pr": caused by this PR's change (its tests, the code it modifies, a compile or lint error in
  files it touches)
- "infra": the CI environment itself (runner lost, disk full, dependency/network download
  failure, OOM of the runner, cancelled setup)
- "unclear": the log doesn't support a call

Answer with JSON only, no prose around it:
{"verdict": "flake|pr|infra|unclear", "confidence": 0.0-1.0,
 "summary": "one sentence: what failed",
 "indicates": "what the failure suggests about the code or the environment",
 "suggested_action": "one sentence: rerun, fix X, ask the author about Y, ..."}"""


def analyze_ci_failures(
    pr: PullRequest,
    forge_client: ForgeClient,
    operator_config: OperatorConfig,
    disabled_backends: frozenset[int] = frozenset(),
) -> int:
    """Analyse every failed check on *pr*'s head not analysed yet. Returns how many."""
    owner, repo = pr.project.owner, pr.project.repo
    checks = forge_client.list_failed_checks(owner, repo, pr.number, pr.head_sha or "")
    if not checks:
        logger.info("PR #%d: no failed CI checks on %s", pr.number, (pr.head_sha or "?")[:12])
        return 0
    # kind too: commit-status ids and check-run ids are separate id spaces.
    done = set(pr.ci_failure_analyses.values_list("check_repo", "check_kind", "check_id"))
    todo = [c for c in checks if (c.repo, c.kind, c.id) not in done]
    if not todo:
        return 0

    # Raised, not returned: 0 would read as "CI is clean" on the dashboard.
    picked = _prompt_backend(operator_config, disabled_backends)
    if picked is None:
        msg = f"{len(todo)} failed CI check(s) left unanalysed: no usable llm_backends entry"
        raise RuntimeError(msg)
    backend_config, backend = picked

    changed = "\n".join((pr.changed_files or [])[:60])
    for check in todo:
        try:
            excerpt = excerpt_log(forge_client.get_check_log(check))
        except Exception:
            logger.warning("Could not fetch the log for %s", check.name, exc_info=True)
            excerpt = check.details
        user = (
            f"PR: {pr.title}\nChanged files:\n{changed or '(unknown)'}\n\n"
            f"Failed check: {check.name} ({check.conclusion}, in {check.repo})\n\n"
            f"Log excerpt:\n{excerpt or '(no log available)'}"
        )
        raw = backend.metered_call(
            _SYSTEM, user, action_type="ci-triage", project_id=pr.project_id, pr_id=pr.pk
        )
        verdict = parse_verdict(raw)
        CIFailureAnalysis.objects.update_or_create(
            pull_request=pr,
            check_repo=check.repo,
            check_kind=check.kind,
            check_id=check.id,
            defaults={
                "head_sha": pr.head_sha or "",
                "check_name": check.name[:500],
                # A fork's commit status sets its own target_url; javascript: would
                # run in the dashboard when clicked.
                "check_url": check.url[:1000]
                if check.url.startswith(("https://", "http://"))
                else "",
                "conclusion": check.conclusion,
                "log_excerpt": excerpt,
                "backend": f"{backend_config.provider}/{backend_config.model}",
                **verdict,
            },
        )
    logger.info("PR #%d: analysed %d failed CI check(s)", pr.number, len(todo))
    return len(todo)


def excerpt_log(log: str) -> str:
    """The lines around errors plus the tail, capped — a Spark job log is megabytes."""
    lines = [_TIMESTAMP.sub("", line) for line in log.splitlines()]
    keep: set[int] = set(range(max(0, len(lines) - _TAIL_LINES), len(lines)))
    for i, line in enumerate(lines):
        if _ERROR_LINE.search(line):
            keep.update(range(max(0, i - _CONTEXT), min(len(lines), i + _CONTEXT + 1)))
    text = "\n".join(lines[i] for i in sorted(keep))
    # The end of a log is where the failure is; cut from the front.
    return text[-_EXCERPT_CHARS:]


def parse_verdict(raw: str) -> dict[str, object]:
    """The model's JSON, defaulted field by field; anything unparseable is "unclear"."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    verdict = str(data.get("verdict", "")).lower()
    valid = {v for v, _ in CIFailureAnalysis.VERDICT_CHOICES}
    try:
        confidence: float | None = max(0.0, min(1.0, float(data["confidence"])))
    except (KeyError, TypeError, ValueError):
        confidence = None
    return {
        "verdict": verdict if verdict in valid else "unclear",
        "confidence": confidence,
        "summary": str(data.get("summary") or (raw or "")[:500]),
        "indicates": str(data.get("indicates") or ""),
        "suggested_action": str(data.get("suggested_action") or ""),
    }


def _prompt_backend(
    operator_config: OperatorConfig, disabled: frozenset[int]
) -> tuple[LLMBackendConfig, BaseLLMBackend] | None:
    """The first live backend that takes a raw prompt (rlm, stub and friends don't)."""
    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.backends.base import BaseLLMBackend

    for i, cfg in enumerate(operator_config.llm_backends):
        if i in disabled or cfg.provider == "stub":
            continue
        backend = get_backend(cfg)
        if isinstance(backend, BaseLLMBackend):
            return cfg, backend
    return None
