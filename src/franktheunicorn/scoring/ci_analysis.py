"""CI-failure analysis — why a failing check is probably a flake or probably real.

The name-prior classifier in :mod:`franktheunicorn.scoring.merge_readiness`
(``classify_ci`` / ``flake_confidence``) only sees a check's *name* and the
project's learned history for it. This module adds the two signals a reviewer
actually uses, and writes them to ``PullRequest.ci_analysis`` for the
classifier to read back:

- **Layer 1 (deterministic): base-branch comparison.** If the same check is
  failing on the base branch right now, the PR did not cause it — the single
  strongest "not your fault" signal, and free (one API call).
- **Layer 2 (opt-in LLM): read the failure.** For each failing check not
  already explained by the base branch, an LLM reads the check's failure
  output against the PR's changed files and judges: a flake, a real failure
  this PR caused, or a real pre-existing failure. One call per check, so it
  runs only on almost-ready candidates or a button press, never every poll.

Every failure degrades to "no analysis" (an empty dict), which leaves the
name-prior classifier exactly as it was.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from franktheunicorn.review.backends.base import _extract_json_blob
from franktheunicorn.scoring.merge_readiness import CICheck, current_checks

if TYPE_CHECKING:
    from franktheunicorn.backends.base import ForgeClient
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)

#: How much of the failure output and diff the LLM sees per check. Bounds the
#: prompt; the check name and summary carry most of the signal.
_MAX_OUTPUT_CHARS = 4_000
_MAX_DIFF_CHARS = 8_000

_VERDICTS = frozenset({"flake", "real-pr", "real-pre-existing"})

_LLM_SYSTEM = """\
You are triaging one failing CI check on a pull request. Decide which of these \
it is, using the failure output, the PR's changed files, whether the same \
check is failing on the base branch, and its past flake history:

- "flake": the failure is not about this PR's code — an infra/network error, a \
timeout, a known-flaky test, or a failure unrelated to the changed files.
- "real-pre-existing": a genuine failure, but it is already failing on the base \
branch (or is clearly unrelated to the diff), so this PR did not cause it.
- "real-pr": a genuine failure that this PR's change caused — the failing test \
or build step exercises code the diff touches.

Return ONLY a JSON object: {"verdict": "flake"|"real-pr"|"real-pre-existing", \
"reason": "<one short sentence>"}."""


def base_failing_checks(client: ForgeClient, owner: str, repo: str, base_sha: str) -> set[str]:
    """Names of checks currently failing on the base branch head, or empty.

    A check failing here too is not evidence about this PR. Any fetch failure
    returns an empty set — a missing base comparison must not invent a signal.
    """
    if not base_sha:
        return set()
    getter = getattr(client, "get_commit_checks", None)
    if not callable(getter):
        return set()
    try:
        raw = getter(owner, repo, base_sha)
    except Exception:
        logger.debug("Could not fetch base-branch checks for %s/%s@%s", owner, repo, base_sha[:12])
        return set()
    failing: set[str] = set()
    for item in raw:
        check = CICheck.from_dict(item) if isinstance(item, dict) else None
        if check is not None and check.failed:
            failing.add(check.name)
    return failing


def _failing_checks(pr: PullRequest) -> list[CICheck]:
    out: list[CICheck] = []
    for item in current_checks(pr):
        check = CICheck.from_dict(item)
        if check is not None and check.failed:
            out.append(check)
    return out


def _llm_verdict_for(
    backend: Any,
    check: CICheck,
    output_text: str,
    diff: str,
    *,
    base_failing: bool,
    history: tuple[int, int],
    project_id: int | None,
    pr_id: int | None,
) -> dict[str, str] | None:
    """One LLM verdict for one failing check, or None on any failure."""
    flaky, real = history
    user = (
        f"Check: {check.name}\n"
        f"Conclusion: {check.conclusion or 'failure'}\n"
        f"Also failing on base branch: {'yes' if base_failing else 'no'}\n"
        f"Past history: flaked {flaky}x, judged real {real}x\n\n"
        f"Failure output:\n{output_text[:_MAX_OUTPUT_CHARS] or '(none available)'}\n\n"
        f"PR changed code (diff):\n{diff[:_MAX_DIFF_CHARS] or '(unavailable)'}"
    )
    try:
        raw = backend.metered_call(
            _LLM_SYSTEM, user, action_type="ci-analysis", project_id=project_id, pr_id=pr_id
        )
    except Exception:
        logger.warning("CI-analysis LLM call failed for check %s.", check.name, exc_info=True)
        return None
    blob = _extract_json_blob(raw)
    if not isinstance(blob, dict):
        return None
    verdict = str(blob.get("verdict", "")).strip()
    if verdict not in _VERDICTS:
        return None
    return {"verdict": verdict, "reason": str(blob.get("reason", "") or ""), "source": "llm"}


def analyze_pr_ci(
    pr: PullRequest,
    client: ForgeClient | None,
    *,
    backend_config: LLMBackendConfig | None = None,
    use_llm: bool = False,
    diff: str = "",
    outputs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Analyse ``pr``'s failing checks and set ``pr.ci_analysis`` (caller saves).

    Layer 1 (base comparison) always runs when a client and base SHA are
    available; Layer 2 (LLM) runs per still-unexplained check when ``use_llm``
    and a backend are given. ``outputs`` maps check name -> failure text for the
    LLM; when absent the LLM works from the name, conclusion, base status, and
    history alone. Returns the stored analysis dict (empty when nothing failed).
    """
    failing = _failing_checks(pr)
    if not failing:
        pr.ci_analysis = {}
        return {}

    failing_names = {c.name for c in failing}
    owner, repo = pr.project.owner, pr.project.repo

    base_failing: set[str] = set()
    if client is not None:
        base_failing = base_failing_checks(client, owner, repo, pr.base_sha) & failing_names

    verdicts: dict[str, dict[str, str]] = {}
    # Base-explained checks get a deterministic verdict without spending a call.
    for name in base_failing:
        verdicts[name] = {
            "verdict": "real-pre-existing",
            "reason": "also failing on the base branch",
            "source": "base",
        }

    if use_llm and backend_config is not None:
        from franktheunicorn.review.backends import get_backend
        from franktheunicorn.scoring.merge_readiness import check_history_for

        backend = get_backend(backend_config)
        if hasattr(backend, "metered_call"):
            history = check_history_for(pr.project)
            if outputs is None and client is not None:
                fetch_outputs = getattr(client, "get_commit_check_outputs", None)
                if callable(fetch_outputs):
                    try:
                        outputs = {
                            str(k): str(v)
                            for k, v in fetch_outputs(owner, repo, pr.head_sha).items()
                        }
                    except Exception:
                        logger.debug(
                            "Could not fetch check outputs for %s/%s#%d", owner, repo, pr.number
                        )
            outputs = outputs or {}
            for check in failing:
                if check.name in verdicts:  # already explained by the base branch
                    continue
                verdict = _llm_verdict_for(
                    backend,
                    check,
                    outputs.get(check.name, ""),
                    diff,
                    base_failing=check.name in base_failing,
                    history=history.get(check.name, (0, 0)),
                    project_id=pr.project_id,
                    pr_id=pr.pk,
                )
                if verdict is not None:
                    verdicts[check.name] = verdict

    analysis: dict[str, Any] = {
        "sha": pr.head_sha,
        "base_failing": sorted(base_failing),
        "verdicts": verdicts,
    }
    pr.ci_analysis = analysis
    logger.info(
        "CI analysis for %s/%s#%d: %d failing, %d base-explained, %d verdict(s)",
        owner,
        repo,
        pr.number,
        len(failing),
        len(base_failing),
        len(verdicts),
    )
    return analysis
