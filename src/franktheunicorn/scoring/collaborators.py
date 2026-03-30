"""Collaborator detection from review history. Pure functions."""

from __future__ import annotations

from franktheunicorn.scoring.signals import WEIGHTS

DEFAULT_THRESHOLD: int = 3


def detect_collaborator(
    author: str,
    operator_username: str,
    review_history: list[dict[str, str]],
    threshold: int = DEFAULT_THRESHOLD,
) -> bool:
    """True if author and operator have reviewed each other >= *threshold* times."""
    if not review_history:
        return False
    a, o = author.lower(), operator_username.lower()
    interactions = sum(
        1
        for e in review_history
        if (e.get("author", "").lower() == a and e.get("reviewer", "").lower() == o)
        or (e.get("author", "").lower() == o and e.get("reviewer", "").lower() == a)
    )
    return interactions >= threshold


def score_collaborator(
    author: str,
    operator_username: str,
    review_history: list[dict[str, str]],
    threshold: int = DEFAULT_THRESHOLD,
) -> float | None:
    """Score boost when the PR author is a detected collaborator."""
    if detect_collaborator(author, operator_username, review_history, threshold):
        return WEIGHTS["collaborator"]
    return None
