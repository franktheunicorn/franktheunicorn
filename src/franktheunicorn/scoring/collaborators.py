"""Scored collaborator detection (§2.4). Pure functions.

Collaborators are scored 0-100, not binary. Boost is proportional to score.
"""

from __future__ import annotations

from franktheunicorn.scoring.signals import WEIGHTS, _lowered


def compute_collaborator_score(
    author: str,
    operator_username: str,
    review_history: list[dict[str, str]],
    frequent_contributors: list[str],
    collaborator_scores: dict[str, float | None] | None = None,
) -> int | None:
    """Scored collaborator boost (0 to WEIGHTS['collaborator'] points).

    Priority: pre-computed scores > frequent_contributors > review history.
    Manual entries (score=None) get full weight.
    """
    author_lower = author.lower()
    weight = WEIGHTS["collaborator"]

    if collaborator_scores:
        for name, score in collaborator_scores.items():
            if name.lower() == author_lower:
                return weight if score is None else round((score / 100) * weight)

    if author_lower in _lowered(frequent_contributors):
        return weight

    if not review_history:
        return None
    op = operator_username.lower()
    mutual = sum(
        1
        for e in review_history
        if (e.get("author", "").lower() == author_lower and e.get("reviewer", "").lower() == op)
        or (e.get("author", "").lower() == op and e.get("reviewer", "").lower() == author_lower)
    )
    if mutual <= 0:
        return None
    crude_score = min(mutual * 20, 100)
    return round((crude_score / 100) * weight)
