"""Blame-based proximity scoring. Pure function."""

from __future__ import annotations

from franktheunicorn.scoring.signals import WEIGHTS


def score_blame_proximity(
    blame_data: list[dict[str, object]],
    operator_username: str,
) -> float | None:
    """Score how many changed files the operator previously touched.

    *blame_data*: ``[{"file_path": str, "authors": [str, ...]}]``.
    Returns weighted fraction or ``None`` when empty / no overlap.
    """
    if not blame_data:
        return None
    op = operator_username.lower()
    hits = sum(
        1
        for entry in blame_data
        if op in {a.lower() for a in entry.get("authors", [])}  # type: ignore[union-attr]
    )
    fraction = hits / len(blame_data)
    return round(WEIGHTS["blame_proximity"] * fraction, 4) if fraction > 0 else None
