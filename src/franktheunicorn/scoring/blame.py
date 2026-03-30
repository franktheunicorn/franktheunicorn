"""Blame-based scoring (§2.5). Pure function.

Layer 1: operator directly authored changed lines (full credit).
Layer 2: operator authored lines near changed lines (half credit).
"""

from __future__ import annotations

from franktheunicorn.scoring.signals import WEIGHTS


def score_touches_operator_code(
    blame_data: list[dict[str, object]],
    operator_username: str,
) -> int | None:
    """Score operator proximity to changed files via blame data.

    blame_data entries: {"file_path": str, "authors": [str], "near_authors": [str]}
    Layer 1: operator in authors -> full credit for that file.
    Layer 2: operator in near_authors only -> half credit.
    Returns None when empty or no overlap.
    """
    if not blame_data:
        return None
    op = operator_username.lower()
    total_credit = 0.0
    for entry in blame_data:
        authors = {a.lower() for a in entry.get("authors", [])}  # type: ignore[union-attr]
        near = {a.lower() for a in entry.get("near_authors", [])}  # type: ignore[union-attr]
        if op in authors:
            total_credit += 1.0
        elif op in near:
            total_credit += 0.5
    fraction = total_credit / len(blame_data)
    if fraction <= 0:
        return None
    return round(WEIGHTS["touches_operator_code"] * fraction)
