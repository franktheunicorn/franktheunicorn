"""Pure scoring signal functions (§2.1). No Django imports.

Weights are integer points on a 0-100 scale, normalized by the orchestrator.
Each function returns int | None (points if signal fires, None to skip).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from fnmatch import fnmatch

WEIGHTS: dict[str, int] = {
    "path_overlap": 30,
    "mentioned_or_assigned": 25,
    "has_review_request": 20,
    "recently_updated": 20,
    "prior_review_history": 15,
    "collaborator": 15,
    "touches_operator_code": 20,
    "merge_conflict": -15,
    "new_human_contributor": 10,
    "keyword_match": 10,
    "ai_generated": 10,
    "llm_interest": 20,
    "committer_is_on_it": -25,
    "updated_since_operator_review": 25,
    "pending_response": 20,
    "downstream_impact": 20,
    "sentry_errors": 15,
    "cve_file_history": 25,
    "draft_findings": 5,
    "mailing_list_mention": 15,
    "mailing_list_blame_author": 10,
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


def _path_matches(file_path: str, pattern: str) -> bool:
    """Check if a file path matches a watch pattern (glob or prefix).

    Supports both glob patterns (``python/pyspark/**``) and simple prefix
    patterns (``sql/catalyst/``).  If the pattern contains glob characters
    (``*``, ``?``, ``[``), :func:`fnmatch` is used; otherwise a prefix
    check is performed for backward compatibility with existing configs.
    """
    if any(c in pattern for c in "*?["):
        return fnmatch(file_path, pattern)
    return file_path.startswith(pattern)


def path_overlap_fraction(changed_files: list[str], watched_paths: list[str]) -> float:
    """Fraction of changed_files matching any watched-path pattern (0.0-1.0)."""
    if not changed_files:
        return 0.0
    matches = sum(1 for f in changed_files if any(_path_matches(f, wp) for wp in watched_paths))
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
    comment_bodies: list[str] | None = None,
) -> int | None:
    """Operator @-mentioned in PR body, a comment, or listed as assignee."""
    op = operator_username.lower()
    if op in _lowered(assignees or []):
        return WEIGHTS["mentioned_or_assigned"]
    pattern = re.compile(rf"@{re.escape(operator_username)}\b", re.IGNORECASE)
    if pattern.search(body or ""):
        return WEIGHTS["mentioned_or_assigned"]
    if comment_bodies:
        for cb in comment_bodies:
            if pattern.search(cb or ""):
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
    """Boost when PR author is a bot or configured AI agent (routes to AI queue for extra review)."""
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
        and any(_path_matches(f, wp) for f in changed_files for wp in watched_paths)
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


def score_cve_file_history(
    changed_files: list[str],
    cve_affected_files: list[str],
) -> int | None:
    """Boost when PR touches files involved in past CVE/security fixes.

    Proportional to the fraction of changed files overlapping with
    CVE-affected files. Auto-detected paths use exact match; manual
    config entries with glob chars or trailing ``/`` use pattern matching.
    """
    if not changed_files or not cve_affected_files:
        return None
    cve_set = set(cve_affected_files)
    # Only use _path_matches for entries that are glob/prefix patterns,
    # not exact file paths (avoids prefix false positives like
    # "src/auth.py" matching "src/auth.py.bak").
    patterns = [p for p in cve_affected_files if any(c in p for c in "*?[") or p.endswith("/")]
    matches = sum(
        1 for f in changed_files if f in cve_set or any(_path_matches(f, p) for p in patterns)
    )
    if matches == 0:
        return None
    fraction = matches / len(changed_files)
    return round(WEIGHTS["cve_file_history"] * fraction)


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


def _parse_iso(dt_str: str) -> datetime:
    """Parse an ISO 8601 datetime string to a timezone-aware datetime."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


_REVIEW_GRACE_PERIOD = timedelta(minutes=5)


def score_updated_since_operator_review(
    operator_review_posted_at: str | None,
    pr_updated_at: str | None,
) -> int | None:
    """Boost when PR was updated after the operator posted a review.

    Compares the operator's most recent posted review timestamp against
    the PR's github_updated_at. A 5-minute grace period prevents the
    operator's own review comment from triggering this signal (posting
    a review bumps github_updated_at).
    """
    if not operator_review_posted_at or not pr_updated_at:
        return None
    try:
        review_dt = _parse_iso(operator_review_posted_at)
        updated_dt = _parse_iso(pr_updated_at)
    except (ValueError, TypeError):
        return None
    if updated_dt > review_dt + _REVIEW_GRACE_PERIOD:
        return WEIGHTS["updated_since_operator_review"]
    return None


def score_pending_response(
    operator_review_posted_at: str | None,
    author_replies_after_review: list[str],
) -> int | None:
    """Boost when the PR author replied after the operator's review.

    author_replies_after_review is a list of ISO 8601 timestamps of
    comments by the PR author posted after the operator's last review.
    If any exist, the author is likely waiting for follow-up.
    """
    if not operator_review_posted_at or not author_replies_after_review:
        return None
    return WEIGHTS["pending_response"]


def score_draft_findings(draft_findings_count: int | None) -> int | None:
    """Light boost when the agent found possible line-level review concerns.

    Surfaces PRs the agent already has something to say about. Boolean signal —
    the count itself drives the dashboard badge, not the score magnitude.
    """
    if not draft_findings_count or draft_findings_count <= 0:
        return None
    return WEIGHTS["draft_findings"]


def score_mailing_list_mention(
    community_context_cache: dict[str, object] | None,
    pr_identifiers: set[str] | None = None,
) -> int | None:
    """Boost when a mailing list thread references this PR or its JIRA ticket.

    ``pr_identifiers`` must contain the PR's own JIRA ticket IDs and ``#<number>``.
    Returns ``None`` when ``pr_identifiers`` is not provided — cannot verify a match
    without knowing which IDs belong to this PR.
    """
    if not community_context_cache or not pr_identifiers:
        return None
    sources = community_context_cache.get("sources", [])
    if not isinstance(sources, list):
        return None
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("type") != "mailing-list":
            continue
        for thread in source.get("threads", []):
            if not isinstance(thread, dict):
                continue
            refs = thread.get("pr_references")
            if isinstance(refs, list) and any(r in pr_identifiers for r in refs):
                return WEIGHTS["mailing_list_mention"]
    return None


def score_mailing_list_blame_author(
    community_context_cache: dict[str, object] | None,
) -> int | None:
    """Boost when a blame author for the changed files appears in a mailing list thread."""
    if not community_context_cache:
        return None
    sources = community_context_cache.get("sources", [])
    if not isinstance(sources, list):
        return None
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("type") != "mailing-list":
            continue
        for thread in source.get("threads", []):
            if not isinstance(thread, dict):
                continue
            if thread.get("blame_hit"):
                return WEIGHTS["mailing_list_blame_author"]
    return None
