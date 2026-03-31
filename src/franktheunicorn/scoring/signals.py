"""Pure scoring signal functions (§2.1). No Django imports.

Weights are integer points on a 0-100 scale, normalized by the orchestrator.
Each function returns int | None (points if signal fires, None to skip).
"""

from __future__ import annotations

import re

WEIGHTS: dict[str, int] = {
    "path_overlap": 30,
    "mentioned_or_assigned": 25,
    "has_review_request": 20,
    "recently_updated": 20,
    "prior_review_history": 15,
    "collaborator": 15,
    "touches_operator_code": 15,
    "merge_conflict": -15,
    "new_human_contributor": 10,
    "keyword_match": 10,
    "ai_generated": -10,
    "llm_interest": 20,
    "committer_is_on_it": -25,
}

MAX_SCORE: int = 100

BOT_PATTERNS: list[str] = [
    r".*\[bot\]$",
    r"^dependabot$",
    r"^renovate$",
    r"^greenkeeper$",
]

LARGE_PR_THRESHOLD: int = 500


def _lowered(items: list[str]) -> set[str]:
    return {s.lower() for s in items}


def is_likely_bot(author: str) -> bool:
    """Check if an author looks like a bot account."""
    return any(re.match(p, author.lower()) for p in BOT_PATTERNS)


def is_ai_agent(author: str, ai_agents: list[str]) -> bool:
    """Check if author matches configured AI agent patterns or bot heuristics."""
    if is_likely_bot(author):
        return True
    return author.lower() in _lowered(ai_agents) if ai_agents else False


def path_overlap_fraction(changed_files: list[str], watched_paths: list[str]) -> float:
    """Fraction of changed_files matching any watched-path prefix (0.0-1.0)."""
    if not changed_files:
        return 0.0
    matches = sum(1 for f in changed_files if any(f.startswith(wp) for wp in watched_paths))
    return matches / len(changed_files)


def score_path_overlap(changed_files: list[str], watched_paths: list[str]) -> int | None:
    if not watched_paths or not changed_files:
        return None
    overlap = path_overlap_fraction(changed_files, watched_paths)
    return round(WEIGHTS["path_overlap"] * overlap) if overlap > 0 else None


def score_mentioned_or_assigned(
    body: str,
    assignees: list[str],
    operator_username: str,
) -> int | None:
    """Operator @-mentioned in PR body or listed as assignee."""
    op = operator_username.lower()
    if op in _lowered(assignees or []):
        return WEIGHTS["mentioned_or_assigned"]
    if re.search(rf"@{re.escape(operator_username)}\b", body or "", re.IGNORECASE):
        return WEIGHTS["mentioned_or_assigned"]
    return None


def score_has_review_request(
    requested_reviewers: list[str],
    operator_username: str,
) -> int | None:
    """Explicit GitHub review request for the operator."""
    if operator_username.lower() in _lowered(requested_reviewers or []):
        return WEIGHTS["has_review_request"]
    return None


def score_prior_review_history(
    author: str,
    operator_username: str,
    review_history: list[dict[str, str]],
) -> int | None:
    """One-directional: has operator previously reviewed this author's PRs?"""
    if not review_history:
        return None
    a, o = author.lower(), operator_username.lower()
    reviewed = any(
        e.get("author", "").lower() == a and e.get("reviewer", "").lower() == o
        for e in review_history
    )
    return WEIGHTS["prior_review_history"] if reviewed else None


def score_new_human_contributor(
    author: str,
    operator_username: str,
    known_authors: list[str],
    ai_agents: list[str] | None = None,
) -> int | None:
    """New contributor bump. Excludes bots, AI agents, known authors, and operator."""
    author_lower = author.lower()
    if (
        is_ai_agent(author, ai_agents or [])
        or author_lower == operator_username.lower()
        or author_lower in _lowered(known_authors)
    ):
        return None
    return WEIGHTS["new_human_contributor"]


def score_keyword_match(title: str, body: str, keywords: list[str]) -> int | None:
    """Case-insensitive keyword match in PR title or body."""
    if not keywords:
        return None
    text = f"{title}\n{body}".lower()
    if any(kw.lower() in text for kw in keywords):
        return WEIGHTS["keyword_match"]
    return None


def score_ai_generated(author: str, ai_agents: list[str] | None = None) -> int | None:
    """Penalty when PR author is a bot or configured AI agent."""
    if is_ai_agent(author, ai_agents or []):
        return WEIGHTS["ai_generated"]
    return None


def score_committer_is_on_it(
    recent_reviews: list[dict[str, str]],
    operator_username: str,
    committers: list[str],
    watched_paths: list[str],
    changed_files: list[str],
    mentioned_or_assigned: bool = False,
    recency_hours: int = 48,
) -> int | None:
    """Down-rank PRs where another committer is actively reviewing (§2.7).

    Conditions for down-ranking (all must be true):
    - A known committer (not operator) has reviewed within *recency_hours*
    - PR is NOT in operator's watch_paths
    - Operator is NOT mentioned or assigned
    """
    if mentioned_or_assigned:
        return None

    # Check if PR touches watched paths — if so, don't derank
    if (
        watched_paths
        and changed_files
        and any(f.startswith(tuple(watched_paths)) for f in changed_files)
    ):
        return None

    committer_set = _lowered(committers)
    op = operator_username.lower()
    committer_set.discard(op)

    if not committer_set:
        return None

    has_active_committer = any(
        e.get("reviewer", "").lower() in committer_set for e in recent_reviews
    )

    return WEIGHTS["committer_is_on_it"] if has_active_committer else None


def score_recently_updated(
    hours_since_update: float | None,
) -> int | None:
    """Boost for recently updated PRs. +20 if updated today, +10 if this week."""
    if hours_since_update is None:
        return None
    today_boost = WEIGHTS["recently_updated"]
    week_boost = today_boost // 2
    if hours_since_update < 24:
        return today_boost
    if hours_since_update < 168:  # 7 days
        return week_boost
    return None


def score_merge_conflict(mergeable: bool | None) -> int | None:
    """Penalty when PR has merge conflicts (not mergeable)."""
    if mergeable is None:
        return None  # unknown status, don't penalize
    return WEIGHTS["merge_conflict"] if not mergeable else None


def score_llm_interest(llm_judgment: str | None) -> int | None:
    """Convert LLM interest judgment to points. high=20, medium=10, low/None=skip."""
    if not llm_judgment:
        return None
    match llm_judgment.strip().lower():
        case "high":
            return WEIGHTS["llm_interest"]
        case "medium":
            return WEIGHTS["llm_interest"] // 2
        case _:
            return None
