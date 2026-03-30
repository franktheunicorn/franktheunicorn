"""Custom scoring expression sandbox with AST validation."""

from __future__ import annotations

import ast
import logging

logger = logging.getLogger(__name__)

_ALLOWED_BUILTINS: dict[str, object] = {
    "len": len,
    "sum": sum,
    "min": min,
    "max": max,
    "abs": abs,
    "any": any,
    "all": all,
    "round": round,
    "True": True,
    "False": False,
    "None": None,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
}

_FORBIDDEN_NODES: tuple[type[ast.AST], ...] = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.ClassDef,
    ast.AsyncFunctionDef,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
)


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

    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        logger.warning("Custom scoring expression has syntax error: %s", expression)
        return None

    # AST safety check
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            logger.warning("Custom scoring expression rejected: %s", expression)
            return None
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            logger.warning("Custom scoring expression rejected (dunder): %s", expression)
            return None

    try:
        result = eval(
            compile(tree, "<custom-score>", "eval"),
            {"__builtins__": _ALLOWED_BUILTINS},
            {"pr": pr, "config": config},
        )
    except Exception:
        logger.warning("Custom scoring expression failed: %s", expression, exc_info=True)
        return None

    try:
        value = float(result)
    except (TypeError, ValueError):
        logger.warning("Custom scoring expression returned non-numeric: %r", result)
        return None

    return max(-1.0, min(1.0, round(value, 4)))
