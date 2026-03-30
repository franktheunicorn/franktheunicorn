"""
Pure scoring signal functions for PR interest scoring.

Each function takes plain data (strings, lists, dicts) and returns
a weighted score (float) or None if the signal doesn't apply.
No Django imports — these are pure functions.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Weights for each scoring signal. Intentionally simple and tunable.
# ---------------------------------------------------------------------------
WEIGHTS: dict[str, float] = {
    "operator_is_author": 0.30,
    "review_requested": 0.25,
    "path_overlap": 0.15,
    "frequent_contributor": 0.10,
    "new_contributor": 0.10,
    "ai_generated_penalty": -0.10,
    "large_pr_penalty": -0.05,
    "blame_proximity": 0.12,
    "collaborator": 0.08,
}

# Heuristic patterns for bot / AI-authored accounts.
BOT_PATTERNS: list[str] = [
    r".*\[bot\]$",
    r"^dependabot$",
    r"^renovate$",
    r"^greenkeeper$",
]

# PRs above this total-change threshold get a penalty.
LARGE_PR_THRESHOLD: int = 500  # additions + deletions


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _lowered_set(items: list[str]) -> set[str]:
    """Build a lowercase set for case-insensitive membership checks."""
    return {s.lower() for s in items}


def is_likely_bot(author: str) -> bool:
    """Check if an author looks like a bot account."""
    lowered = author.lower()
    return any(re.match(pattern, lowered) for pattern in BOT_PATTERNS)


def path_overlap_fraction(
    changed_files: list[str],
    watched_paths: list[str],
) -> float:
    """Compute what fraction of *changed_files* match any watched-path prefix.

    Returns a value between 0.0 and 1.0.
    """
    if not changed_files:
        return 0.0
    matches = sum(1 for f in changed_files if any(f.startswith(wp) for wp in watched_paths))
    return matches / len(changed_files)


# ---------------------------------------------------------------------------
# Individual signal functions
# ---------------------------------------------------------------------------


def score_operator_is_author(author: str, operator_username: str) -> float | None:
    """Operator authored the PR — always high interest."""
    if author.lower() == operator_username.lower():
        return WEIGHTS["operator_is_author"]
    return None


def score_review_requested(
    requested_reviewers: list[str],
    operator_username: str,
) -> float | None:
    """Operator was explicitly requested as a reviewer."""
    if operator_username.lower() in _lowered_set(requested_reviewers or []):
        return WEIGHTS["review_requested"]
    return None


def score_path_overlap(
    changed_files: list[str],
    watched_paths: list[str],
) -> float | None:
    """Fraction of changed files that match watched path prefixes."""
    if not watched_paths or not changed_files:
        return None
    overlap = path_overlap_fraction(changed_files, watched_paths)
    if overlap > 0:
        return round(WEIGHTS["path_overlap"] * overlap, 4)
    return None


def score_frequent_contributor(
    author: str,
    frequent_contributors: list[str],
) -> float | None:
    """Author is on the project's frequent-contributors list."""
    if author.lower() in _lowered_set(frequent_contributors):
        return WEIGHTS["frequent_contributor"]
    return None


def score_new_contributor(
    author: str,
    operator_username: str,
    frequent_contributors: list[str],
    known_authors: list[str],
) -> float | None:
    """Brand-new contributor bump.

    Only fires when the author is *not* a bot, *not* known,
    *not* the operator, and has no prior PRs in the project
    (represented by ``known_authors``).
    """
    author_lower = author.lower()
    if is_likely_bot(author):
        return None
    if author_lower in _lowered_set(frequent_contributors):
        return None
    if author_lower == operator_username.lower():
        return None
    if author_lower in _lowered_set(known_authors):
        return None
    return WEIGHTS["new_contributor"]


def score_ai_generated(author: str) -> float | None:
    """Penalty for bot / AI-generated PRs."""
    if is_likely_bot(author):
        return WEIGHTS["ai_generated_penalty"]
    return None


def score_large_pr(
    additions: int,
    deletions: int,
    threshold: int = LARGE_PR_THRESHOLD,
) -> float | None:
    """Penalty for very large PRs that are harder to review well."""
    if (additions + deletions) > threshold:
        return WEIGHTS["large_pr_penalty"]
    return None
