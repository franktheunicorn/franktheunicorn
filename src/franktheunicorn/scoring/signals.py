"""Pure scoring signal functions. No Django imports."""

from __future__ import annotations

import re

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


def path_overlap_fraction(changed_files: list[str], watched_paths: list[str]) -> float:
    """Fraction of changed_files matching any watched-path prefix (0.0-1.0)."""
    if not changed_files:
        return 0.0
    matches = sum(1 for f in changed_files if any(f.startswith(wp) for wp in watched_paths))
    return matches / len(changed_files)


def score_operator_is_author(author: str, operator_username: str) -> float | None:
    if author.lower() == operator_username.lower():
        return WEIGHTS["operator_is_author"]
    return None


def score_review_requested(requested_reviewers: list[str], operator_username: str) -> float | None:
    if operator_username.lower() in _lowered(requested_reviewers or []):
        return WEIGHTS["review_requested"]
    return None


def score_path_overlap(changed_files: list[str], watched_paths: list[str]) -> float | None:
    if not watched_paths or not changed_files:
        return None
    overlap = path_overlap_fraction(changed_files, watched_paths)
    return round(WEIGHTS["path_overlap"] * overlap, 4) if overlap > 0 else None


def score_frequent_contributor(author: str, frequent_contributors: list[str]) -> float | None:
    if author.lower() in _lowered(frequent_contributors):
        return WEIGHTS["frequent_contributor"]
    return None


def score_new_contributor(
    author: str,
    operator_username: str,
    frequent_contributors: list[str],
    known_authors: list[str],
) -> float | None:
    """New contributor bump. Excludes bots, known contributors, and the operator."""
    author_lower = author.lower()
    if (
        is_likely_bot(author)
        or author_lower in _lowered(frequent_contributors)
        or author_lower == operator_username.lower()
        or author_lower in _lowered(known_authors)
    ):
        return None
    return WEIGHTS["new_contributor"]


def score_ai_generated(author: str) -> float | None:
    return WEIGHTS["ai_generated_penalty"] if is_likely_bot(author) else None


def score_large_pr(
    additions: int, deletions: int, threshold: int = LARGE_PR_THRESHOLD
) -> float | None:
    return WEIGHTS["large_pr_penalty"] if (additions + deletions) > threshold else None
