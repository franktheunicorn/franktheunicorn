"""
Collaborator detection for PR interest scoring.

Detects whether a PR author is a frequent collaborator of the operator
based on review-interaction history. Pure functions — no ORM, no side effects.
"""

from __future__ import annotations

from franktheunicorn.scoring.signals import WEIGHTS

DEFAULT_THRESHOLD: int = 3


def detect_collaborator(
    author: str,
    operator_username: str,
    review_history: list[dict[str, str]],
    threshold: int = DEFAULT_THRESHOLD,
) -> bool:
    """Return ``True`` if *author* and the operator have reviewed each other's
    PRs at least *threshold* times in total.

    Parameters
    ----------
    author:
        GitHub username of the PR author.
    operator_username:
        GitHub username of the operator.
    review_history:
        List of past PR review interactions, each with
        ``{"author": str, "reviewer": str}`` representing one review event.
    threshold:
        Minimum number of mutual interactions required.
    """
    if not review_history:
        return False

    author_lower = author.lower()
    op_lower = operator_username.lower()

    interactions = sum(
        1
        for entry in review_history
        if (
            # Operator reviewed author's PR
            (
                entry.get("author", "").lower() == author_lower
                and entry.get("reviewer", "").lower() == op_lower
            )
            # Author reviewed operator's PR
            or (
                entry.get("author", "").lower() == op_lower
                and entry.get("reviewer", "").lower() == author_lower
            )
        )
    )

    return interactions >= threshold


def score_collaborator(
    author: str,
    operator_username: str,
    review_history: list[dict[str, str]],
    threshold: int = DEFAULT_THRESHOLD,
) -> float | None:
    """Score boost when the PR author is a detected collaborator.

    Returns the ``collaborator`` weight if the author qualifies, else ``None``.
    """
    if detect_collaborator(author, operator_username, review_history, threshold):
        return WEIGHTS["collaborator"]
    return None
