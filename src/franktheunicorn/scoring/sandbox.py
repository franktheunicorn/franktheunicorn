"""Custom scoring expression sandbox using simpleeval."""

from __future__ import annotations

import logging

from simpleeval import EvalWithCompoundTypes, InvalidExpression  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_SAFE_FUNCTIONS = {
    "len": len,
    "sum": sum,
    "min": min,
    "max": max,
    "abs": abs,
    "any": any,
    "all": all,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
}


def evaluate_custom_score(
    expression: str,
    pr: dict[str, object],
    config: dict[str, object],
) -> float | None:
    """Evaluate a scoring expression with access to *pr* and *config* dicts.

    Returns float clamped to [-1.0, 1.0], or None on any error.
    """
    if not expression or not expression.strip():
        return None

    evaluator = EvalWithCompoundTypes(
        functions=_SAFE_FUNCTIONS,
        names={"pr": pr, "config": config},
    )

    try:
        result = evaluator.eval(expression.strip())
    except (InvalidExpression, SyntaxError, TypeError, ValueError, KeyError, AttributeError):
        logger.warning("Custom scoring expression rejected: %s", expression)
        return None
    except Exception:
        logger.warning("Custom scoring expression failed: %s", expression, exc_info=True)
        return None

    try:
        value = float(result)
    except (TypeError, ValueError):
        logger.warning("Custom scoring expression returned non-numeric: %r", result)
        return None

    return max(-1.0, min(1.0, round(value, 4)))
