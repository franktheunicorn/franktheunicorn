"""
Custom scoring expression sandbox.

Allows operators to write small Python expressions that receive PR and config
data and return a float score. Expressions are AST-validated to reject
dangerous constructs (imports, dunder access, arbitrary calls).

Pure function — no Django imports, no side effects beyond logging.
"""

from __future__ import annotations

import ast
import logging

logger = logging.getLogger(__name__)

# Builtins available inside custom expressions.
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

# AST node types that are never allowed.
_FORBIDDEN_NODE_TYPES: tuple[type[ast.AST], ...] = (
    ast.Import,
    ast.ImportFrom,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
    ast.AsyncFunctionDef,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
    ast.ClassDef,
    ast.FunctionDef,
)


def _validate_ast(tree: ast.AST) -> str | None:
    """Walk the AST and return an error message if anything unsafe is found,
    or ``None`` if the expression is acceptable."""
    for node in ast.walk(tree):
        # Block forbidden node types
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            return f"forbidden construct: {type(node).__name__}"

        # Block dunder attribute access
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return f"dunder attribute access: {node.attr}"

    return None


def evaluate_custom_score(
    expression: str,
    pr: dict[str, object],
    config: dict[str, object],
) -> float | None:
    """Evaluate a custom scoring expression in a restricted sandbox.

    Parameters
    ----------
    expression:
        A Python expression that may reference ``pr`` and ``config``.
        Must evaluate to a number.
    pr:
        PR data dict (author, changed_files, additions, etc.).
    config:
        Project config dict (watched_paths, frequent_contributors, etc.).

    Returns
    -------
    float | None
        The result clamped to [-1.0, 1.0], or ``None`` on any error.
    """
    if not expression or not expression.strip():
        return None

    # Parse
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        logger.warning("Custom scoring expression has syntax error: %s", expression)
        return None

    # Validate AST
    violation = _validate_ast(tree)
    if violation is not None:
        logger.warning("Custom scoring expression rejected (%s): %s", violation, expression)
        return None

    # Evaluate in restricted namespace
    restricted_globals: dict[str, object] = {"__builtins__": _ALLOWED_BUILTINS}
    restricted_locals: dict[str, object] = {"pr": pr, "config": config}

    try:
        result = eval(
            compile(tree, "<custom-score>", "eval"),
            restricted_globals,
            restricted_locals,
        )
    except Exception:
        logger.warning("Custom scoring expression failed: %s", expression, exc_info=True)
        return None

    # Coerce to float and clamp
    try:
        value = float(result)
    except (TypeError, ValueError):
        logger.warning(
            "Custom scoring expression returned non-numeric: %r from %s",
            result,
            expression,
        )
        return None

    return max(-1.0, min(1.0, round(value, 4)))
