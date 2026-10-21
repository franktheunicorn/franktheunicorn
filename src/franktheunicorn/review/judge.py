"""
Second-level review judge (§4) — a cheaper model screens draft comments.

The main review backend tends to over-produce: vague nits ("worth a thought"),
malformed fragments (a sentence that lost its leading subject), claims the diff
does not actually support, and near-duplicates of each other. Rejecting those
one draft at a time is the operator's time, so a second model looks at the
findings *before* they become drafts and says which are worth posting.

Two tiers, both opt-in via :class:`JudgeConfig`:

- **Tier A (batch):** one call sees the diff and every surviving finding and
  returns a keep/drop verdict per finding, may lightly tighten a kept comment's
  wording, and flags the ones it is unsure about as ``borderline``. One call per
  PR, so it is cheap even on a cheaper model, and it is the only tier that can
  see findings side by side (which is what catches semantic near-duplicates).
- **Tier B (verify):** for each ``borderline`` finding, a focused adversarial
  call asks whether that single claim is actually true of the code it targets.
  Capped per PR so a flood of borderline findings cannot run up the bill.

The judge never *adds* findings and never rewrites a claim into something new;
the worst it can do when it misbehaves is drop a good comment or keep a weak
one, both of which the operator still sees and can act on (drafts are
draft-only). Every failure path returns "keep unchanged", so a dead or
misconfigured judge backend degrades to the pre-judge behaviour.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import (
    _coerce_prose,
    _extract_json_blob,
    _is_nonsense_body,
    json_object_candidates,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.review.backends.base import PRContext, ReviewFinding

logger = logging.getLogger(__name__)

#: How much of the diff the batch judge sees. The judge decides whether a
#: comment is supported by the change, so it needs the change — but a Spark diff
#: can be enormous, and the findings already name their file and line. A generous
#: window that still bounds the prompt.
_MAX_DIFF_CHARS = 24_000

_CRITERIA = """\
DROP a comment that is:
- not clearly supported by the diff (the claim is speculative or wrong),
- vague or non-actionable ("worth a thought", "a lot of manual state
  management", "we should ensure this is safe" with no specific concern),
- a pure affirmation that asks for no change — it praises the code, confirms it
  is correct, or only restates what it already does ("This looks solid", "The
  finally block correctly releases the connection", "this is what we need").
  Approval belongs in the review summary, not a line comment,
- a malformed fragment (e.g. it starts mid-sentence with no subject),
- redundant with another comment shown (keep the clearest one),
- pure style noise the project would not want raised.

KEEP a comment that makes a specific, correct, actionable point — a real bug,
a concrete risk, a question a reviewer would genuinely ask.

You MAY lightly tighten a kept comment's wording: fix a dropped subject, cut a
redundant clause. Do NOT change its technical claim, add praise or hedges, or
turn it into a different comment. Leave it unchanged if it is already fine.

Mark "borderline": true when you are not sure and a closer look at the code
would settle it."""

_BATCH_SYSTEM = f"""\
You are a senior engineer screening draft code-review comments before a
maintainer sees them. For each draft comment, decide whether it is worth
posting on THIS pull request.

{_CRITERIA}

Return ONLY a JSON array with EXACTLY ONE object per comment — if there are N
comments, return N objects — each carrying the comment's 0-based "index":
[{{"index": 0, "keep": true, "reason": "...", "revised": "", "borderline": false}},
 {{"index": 1, "keep": false, "reason": "...", "revised": "", "borderline": false}}]
Use "revised": "" to keep the wording unchanged. Output only the array — no
markdown fences, no commentary, no reasoning before or after it."""

_ONE_SYSTEM = f"""\
You are a senior engineer screening one draft code-review comment before a
maintainer sees it. Decide whether it is worth posting on THIS pull request.

{_CRITERIA}

Return ONLY a JSON object, nothing else:
{{"keep": true, "reason": "...", "revised": "", "borderline": false}}"""

_VERIFY_SYSTEM = """\
You are verifying one draft code-review comment against the code it targets.
Decide whether the comment's claim is actually correct and worth posting on
this pull request. Be adversarial: if the claim is speculative, already handled
by the surrounding code, or cannot be judged from what is shown, drop it.

Return ONLY a JSON object: {"keep": true|false, "reason": "..."}."""


@dataclass
class JudgeVerdict:
    """The judge's decision for one finding, aligned 1:1 with the input list."""

    keep: bool
    reason: str = ""
    #: A tightened body, or "" to keep the finding's own body. Only applied when
    #: it survives coercion and is not nonsense.
    revised_body: str = ""
    #: Tier A was unsure; tier B (if enabled) took a closer look.
    borderline: bool = False
    #: Which tier produced this verdict, for logging/telemetry.
    tier: str = "batch"


def _keep_all(findings: list[ReviewFinding], reason: str) -> list[JudgeVerdict]:
    return [JudgeVerdict(keep=True, reason=reason, tier="skipped") for _ in findings]


def _truncate_diff(diff: str) -> str:
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    return diff[:_MAX_DIFF_CHARS] + "\n… (diff truncated)"


def _render_findings(findings: list[ReviewFinding]) -> str:
    lines: list[str] = []
    for i, f in enumerate(findings):
        loc = f.file_path or "(no file)"
        if f.line_number:
            loc += f":{f.line_number}"
        lines.append(f"[{i}] {loc}\n{f.body}")
    return "\n\n".join(lines)


def _get_metered_backend(backend_config: LLMBackendConfig | None) -> object | None:
    """Resolve a backend able to make metered calls, or None to skip judging."""
    if backend_config is None or backend_config.provider == "stub":
        return None
    from franktheunicorn.review.backends import get_backend

    backend = get_backend(backend_config)
    if not hasattr(backend, "metered_call"):
        return None
    return backend


def _batch_items(raw: str) -> tuple[list[dict[str, object]], bool] | None:
    """The verdict objects in *raw*, and whether they are in order.

    The clean case is a JSON array (ordered); a smaller model often returns a
    bare object (one verdict), objects separated by newlines or prose, or the
    array fenced in markdown. Prefer the array, then fall back to harvesting
    every balanced ``{...}`` — those are unordered, so each must carry its own
    ``index``. ``None`` means nothing parseable was found at all.
    """
    blob = _extract_json_blob(raw)
    if isinstance(blob, list):
        items = [x for x in blob if isinstance(x, dict)]
        if items:
            return items, True
    harvested: list[dict[str, object]] = []
    for cand in json_object_candidates(raw):
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict) and "keep" in obj:
            harvested.append(obj)
    return (harvested, False) if harvested else None


def _parse_batch_verdicts(raw: str, n: int) -> list[JudgeVerdict] | None:
    """Parse the batch judge's response into n verdicts, or None if unusable."""
    result = _batch_items(raw)
    if result is None:
        return None
    items, ordered = result
    # Index by the model's own "index" field when present. For a clean (ordered)
    # array we fall back to position; harvested objects are unordered, so one
    # without an index is dropped rather than guessed onto the wrong finding.
    verdicts: list[JudgeVerdict | None] = [None] * n
    for pos, item in enumerate(items):
        raw_idx = item.get("index")
        idx = raw_idx if isinstance(raw_idx, int) else (pos if ordered else None)
        if not isinstance(idx, int) or not (0 <= idx < n):
            continue
        revised = _coerce_prose(str(item.get("revised", "") or "")).strip()
        if _is_nonsense_body(revised):
            revised = ""
        verdicts[idx] = JudgeVerdict(
            keep=bool(item.get("keep", True)),
            reason=str(item.get("reason", "") or ""),
            revised_body=revised,
            borderline=bool(item.get("borderline", False)),
            tier="batch",
        )
    # A finding the batch said nothing about is marked "unruled" so the caller
    # can re-ask about it one-on-one (small models often answer only the first
    # comment of a batch). If that fallback is off, an unruled finding is kept —
    # silence is never a rejection.
    return [v or JudgeVerdict(keep=True, reason="no verdict", tier="unruled") for v in verdicts]


def _finding_location(finding: ReviewFinding) -> str:
    loc = finding.file_path or "(no file)"
    if finding.line_number:
        loc += f":{finding.line_number}"
    return loc


def _judge_one(
    backend: object,
    finding: ReviewFinding,
    diff: str,
    *,
    project_id: int | None,
    pr_id: int | None,
) -> JudgeVerdict:
    """Per-finding batch-criteria judgement, for a finding the batch left unruled.

    A smaller model often answers only the first comment of a batch; asking
    about the rest one at a time is reliable. Returns keep-unchanged on any
    parse or call failure.
    """
    user = (
        f"Comment on {_finding_location(finding)}:\n{finding.body}\n\nDiff:\n{_truncate_diff(diff)}"
    )
    try:
        raw = backend.metered_call(  # type: ignore[attr-defined]
            _ONE_SYSTEM,
            user,
            action_type="judge-one",
            project_id=project_id,
            pr_id=pr_id,
        )
    except Exception:
        logger.warning("Judge per-finding call failed; keeping finding.", exc_info=True)
        return JudgeVerdict(keep=True, reason="per-finding call failed", tier="one")

    blob = _extract_json_blob(raw)
    if not isinstance(blob, dict):
        return JudgeVerdict(keep=True, reason="unparseable per-finding", tier="one")
    revised = _coerce_prose(str(blob.get("revised", "") or "")).strip()
    if _is_nonsense_body(revised):
        revised = ""
    return JudgeVerdict(
        keep=bool(blob.get("keep", True)),
        reason=str(blob.get("reason", "") or ""),
        revised_body=revised,
        borderline=bool(blob.get("borderline", False)),
        tier="one",
    )


def _verify_one(
    backend: object,
    finding: ReviewFinding,
    diff: str,
    *,
    project_id: int | None,
    pr_id: int | None,
) -> JudgeVerdict:
    """Tier B: focused adversarial check of a single borderline finding."""
    user = (
        f"Comment on {_finding_location(finding)}:\n{finding.body}\n\nDiff:\n{_truncate_diff(diff)}"
    )
    try:
        raw = backend.metered_call(  # type: ignore[attr-defined]
            _VERIFY_SYSTEM,
            user,
            action_type="judge-verify",
            project_id=project_id,
            pr_id=pr_id,
        )
    except Exception:
        logger.warning("Judge verify call failed; keeping borderline finding.", exc_info=True)
        return JudgeVerdict(keep=True, reason="verify failed", borderline=True, tier="verify")

    blob = _extract_json_blob(raw)
    if not isinstance(blob, dict):
        return JudgeVerdict(keep=True, reason="unparseable verify", borderline=True, tier="verify")
    return JudgeVerdict(
        keep=bool(blob.get("keep", True)),
        reason=str(blob.get("reason", "") or ""),
        borderline=True,
        tier="verify",
    )


def judge_findings(
    findings: list[ReviewFinding],
    pr_context: PRContext,
    backend_config: LLMBackendConfig | None,
    *,
    diff: str = "",
    verify_borderline: bool = True,
    max_verify: int = 5,
    project_id: int | None = None,
    pr_id: int | None = None,
) -> list[JudgeVerdict]:
    """Judge *findings*, returning a verdict per finding (aligned 1:1).

    Tier A runs one batch call; tier B re-checks each borderline finding when
    ``verify_borderline`` is set, up to ``max_verify`` calls. Any failure keeps
    the finding unchanged — the judge only ever trims, it never blocks review.
    """
    if not findings:
        return []

    backend = _get_metered_backend(backend_config)
    if backend is None:
        return _keep_all(findings, "judge backend unavailable")

    user = (
        f"Pull request diff:\n{_truncate_diff(diff)}\n\n"
        f"There are {len(findings)} draft comments; return {len(findings)} verdict "
        f"object(s).\n\nDraft comments:\n" + _render_findings(findings)
    )
    try:
        raw = backend.metered_call(  # type: ignore[attr-defined]
            _BATCH_SYSTEM,
            user,
            action_type="judge-batch",
            project_id=project_id,
            pr_id=pr_id,
        )
    except Exception:
        logger.warning("Judge batch call failed; keeping all findings.", exc_info=True)
        return _keep_all(findings, "batch call failed")

    verdicts = _parse_batch_verdicts(raw, len(findings))
    if verdicts is None:
        logger.warning("Judge batch response was not a JSON array; keeping all findings.")
        return _keep_all(findings, "unparseable batch response")

    # A finding the batch did not rule on (a small model often answers only the
    # first) is re-asked one-on-one, which such models handle reliably.
    for i, verdict in enumerate(verdicts):
        if verdict.tier == "unruled":
            verdicts[i] = _judge_one(backend, findings[i], diff, project_id=project_id, pr_id=pr_id)

    if verify_borderline and max_verify > 0:
        spent = 0
        for i, verdict in enumerate(verdicts):
            # Only spend a verify call where it can change the outcome: a
            # borderline finding the batch tier is still keeping.
            if not (verdict.borderline and verdict.keep):
                continue
            if spent >= max_verify:
                break
            spent += 1
            check = _verify_one(backend, findings[i], diff, project_id=project_id, pr_id=pr_id)
            # Keep the batch tier's tightened wording even if verify confirms it.
            check.revised_body = verdict.revised_body
            verdicts[i] = check

    dropped = sum(1 for v in verdicts if not v.keep)
    logger.info(
        "Judge: %d finding(s) in, %d dropped, %d kept (%d borderline-verified).",
        len(findings),
        dropped,
        len(findings) - dropped,
        sum(1 for v in verdicts if v.tier == "verify"),
    )
    return verdicts


def apply_verdicts(
    findings: list[ReviewFinding],
    verdicts: list[JudgeVerdict],
) -> tuple[list[ReviewFinding], list[int]]:
    """Return the kept findings (with any revised body applied) and their indices.

    The indices let the caller filter parallel lists (sources, categories) so
    everything stays aligned after the judge drops a finding.
    """
    kept: list[ReviewFinding] = []
    kept_indices: list[int] = []
    for i, (finding, verdict) in enumerate(zip(findings, verdicts, strict=False)):
        if not verdict.keep:
            continue
        if verdict.revised_body:
            finding = finding.model_copy(update={"body": verdict.revised_body})
        kept.append(finding)
        kept_indices.append(i)
    return kept, kept_indices
