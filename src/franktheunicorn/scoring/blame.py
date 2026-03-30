"""
Blame-based proximity scoring.

Scores how much of the changed code was previously touched by the operator,
indicating familiarity and review relevance. Pure function — takes pre-computed
blame summaries as plain dicts.
"""

from __future__ import annotations

from franktheunicorn.scoring.signals import WEIGHTS


def score_blame_proximity(
    blame_data: list[dict[str, object]],
    operator_username: str,
) -> float | None:
    """Score operator proximity to changed files via blame data.

    Parameters
    ----------
    blame_data:
        List of per-file blame summaries, each with:
        ``{"file_path": str, "authors": list[str]}``
        where *authors* are the distinct users who last-touched lines in that file.
    operator_username:
        The GitHub username of the operator.

    Returns
    -------
    float | None
        Weighted score (0 … WEIGHTS["blame_proximity"]) proportional to the
        fraction of files where the operator appears in the blame, or ``None``
        when *blame_data* is empty / not provided.
    """
    if not blame_data:
        return None

    op_lower = operator_username.lower()
    files_with_operator = sum(
        1
        for entry in blame_data
        if op_lower in [a.lower() for a in entry.get("authors", [])]  # type: ignore[union-attr]
    )

    fraction = files_with_operator / len(blame_data)
    if fraction <= 0:
        return None

    return round(WEIGHTS["blame_proximity"] * fraction, 4)
