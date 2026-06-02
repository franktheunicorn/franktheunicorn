"""Reduce step: merge per-leaf review results into one.

Aggregation is deterministic (modulo the leaf LLM outputs themselves): leaf
findings are concatenated and run through the shared
:func:`deduplicate_findings` so near-duplicate findings emitted across hunks
of the same file collapse before they reach the drafter's own dedup pass.
"""

from __future__ import annotations

from franktheunicorn.review.backends.base import ReviewFinding, ReviewResult
from franktheunicorn.review.dedup import deduplicate_findings

# Severities that mark a PR as clearly worth the operator's attention.
_HIGH_INTEREST_SEVERITIES = frozenset({"critical", "important", "high"})


def merge_vibes(vibes: list[str]) -> str:
    """Deterministically combine per-leaf vibe summaries into one string."""
    seen: set[str] = set()
    unique: list[str] = []
    for vibe in vibes:
        v = vibe.strip()
        if v and v not in seen:
            seen.add(v)
            unique.append(v)
    return " ".join(unique[:5])


def aggregate_review(
    results: list[ReviewResult],
    *,
    synthesized_vibe: str = "",
) -> ReviewResult:
    """Combine leaf ``ReviewResult``s into a single deduplicated result."""
    findings: list[ReviewFinding] = [f for r in results for f in r.findings]
    deduped = deduplicate_findings(findings)
    vibe = synthesized_vibe or merge_vibes([r.overall_vibe for r in results])
    return ReviewResult(overall_vibe=vibe, findings=deduped)


def interest_label_from_findings(findings: list[ReviewFinding]) -> str | None:
    """Map an aggregated review into an ``llm_interest`` label.

    Returns ``"high"``/``"medium"`` (consumed by ``score_llm_interest``) or
    ``None`` to skip the signal entirely when nothing substantive surfaced.
    """
    if not findings:
        return None
    if any(f.severity.lower() in _HIGH_INTEREST_SEVERITIES for f in findings):
        return "high"
    if len(findings) >= 3:
        return "high"
    return "medium"
