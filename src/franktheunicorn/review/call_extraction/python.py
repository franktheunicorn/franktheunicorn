"""Extract Python call sites from a unified diff.

Strategy: parse the diff with unidiff, take the added (post-image) lines for
each .py file, and run :mod:`ast` over a synthetic module composed of those
lines. We resolve calls to a fully-qualified ``package.module.func`` by
tracking ``import`` and ``from ... import`` statements in the same hunk
window. Stdlib modules and the project's own package are filtered out so
the api-misuse check only fans out to third-party docs.

The extractor is deliberately tolerant: lines that fail to parse as a
standalone snippet (because they reference symbols defined elsewhere)
fall back to a regex pass that catches ``module.func(`` patterns. Both
paths are best-effort; the consumer treats results as hints, not facts.
"""

from __future__ import annotations

import ast
import logging
import re
import sys
from dataclasses import dataclass

from unidiff import PatchSet  # type: ignore[import-untyped]

from franktheunicorn.review.call_extraction.types import CallSite, Language

logger = logging.getLogger(__name__)

# Python 3.10+ exposes the canonical stdlib name set.
_STDLIB: frozenset[str] = frozenset(sys.stdlib_module_names) | frozenset(
    {"__future__", "typing_extensions"}
)


def _builtin_names() -> frozenset[str]:
    """Best-effort enumeration of Python builtin names."""
    import builtins

    return frozenset(dir(builtins))


# Builtins we never want to surface as "external API misuse" candidates.
_BUILTINS: frozenset[str] = _builtin_names()

_DOTTED_CALL_RE = re.compile(
    r"\b([a-zA-Z_][\w\.]{2,})\s*\("  # module.func( — at least one dot
)


@dataclass(frozen=True)
class _ImportBinding:
    """Maps a local name to ``(top_level_package, qualified_name)``."""

    package: str
    qualified: str


def extract_python_calls(diff: str, *, project_package: str = "") -> list[CallSite]:
    """Return external Python call sites observed in the diff.

    ``project_package`` is the operator's first-party top-level package name
    (e.g. ``franktheunicorn``); calls under it are dropped.
    """
    try:
        patch = PatchSet(diff)
    except Exception:
        logger.debug("Failed to parse diff as PatchSet", exc_info=True)
        return []

    sites: list[CallSite] = []
    for pf in patch:
        path = getattr(pf, "path", "") or getattr(pf, "target_file", "")
        if not path.endswith(".py"):
            continue
        for hunk in pf:
            sites.extend(_extract_from_hunk(path, hunk, project_package))
    return _dedupe(sites)


def _extract_from_hunk(path: str, hunk: object, project_package: str) -> list[CallSite]:
    added: list[tuple[int, str]] = []
    for line in hunk:  # type: ignore[attr-defined]
        if line.is_added:
            added.append((line.target_line_no, line.value.rstrip("\n")))

    if not added:
        return []

    bindings = _collect_imports(added)
    sites: list[CallSite] = []

    for line_no, source in added:
        for pkg, qualified, snippet in _calls_in_line(source, bindings):
            if _should_skip(pkg, project_package):
                continue
            sites.append(
                CallSite(
                    language=Language.PYTHON,
                    package=pkg,
                    qualified_name=qualified,
                    file_path=path,
                    line_number=line_no,
                    snippet=snippet,
                )
            )
    return sites


def _collect_imports(added: list[tuple[int, str]]) -> dict[str, _ImportBinding]:
    """Walk added lines, parse import statements one-by-one, build a name→binding map."""
    bindings: dict[str, _ImportBinding] = {}
    for _, source in added:
        stripped = source.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        try:
            tree = ast.parse(stripped)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name
                    top = alias.name.split(".", 1)[0]
                    bindings[name] = _ImportBinding(top, alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                top = module.split(".", 1)[0] if module else ""
                if not top and node.level:
                    # Relative import — treat as first-party, not extractable.
                    continue
                for alias in node.names:
                    name = alias.asname or alias.name
                    qualified = f"{module}.{alias.name}" if module else alias.name
                    bindings[name] = _ImportBinding(top, qualified)
    return bindings


def _calls_in_line(source: str, bindings: dict[str, _ImportBinding]) -> list[tuple[str, str, str]]:
    """Yield ``(package, qualified_name, snippet)`` for each external call on a line."""
    snippet = source.strip()
    results: list[tuple[str, str, str]] = []

    # AST path — handles ``module.func(...)`` and ``alias.method(...)`` cleanly
    # when the line is parseable in isolation (e.g. a single statement).
    try:
        tree = ast.parse(snippet)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            qualified = _resolve_call(node.func, bindings)
            if qualified is None:
                continue
            pkg, name = qualified
            results.append((pkg, name, snippet))

    # Regex fallback — catches calls inside expressions/lines that don't
    # parse standalone (e.g. lines added inside an existing function body
    # where indentation / surrounding context is missing).
    if not results:
        for match in _DOTTED_CALL_RE.finditer(snippet):
            dotted = match.group(1)
            head, _, _ = dotted.partition(".")
            if head not in bindings:
                continue
            binding = bindings[head]
            tail = dotted.split(".", 1)[1] if "." in dotted else ""
            qname = f"{binding.qualified}.{tail}" if tail else binding.qualified
            results.append((binding.package, qname, snippet))

    return results


def _resolve_call(func: ast.expr, bindings: dict[str, _ImportBinding]) -> tuple[str, str] | None:
    """Walk an ``ast.Call.func`` expression to a ``(package, qualified)`` pair."""
    parts: list[str] = []
    cur: ast.expr | None = func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        return None
    parts.reverse()

    head = parts[0]
    if head not in bindings:
        return None
    binding = bindings[head]
    if len(parts) == 1:
        return binding.package, binding.qualified
    qualified = binding.qualified + "." + ".".join(parts[1:])
    return binding.package, qualified


def _should_skip(package: str, project_package: str) -> bool:
    if not package:
        return True
    if package in _STDLIB:
        return True
    if package in _BUILTINS:
        return True
    return bool(project_package and package == project_package)


def _dedupe(sites: list[CallSite]) -> list[CallSite]:
    """Drop exact duplicates while preserving first-seen order."""
    seen: set[tuple[str, str, str, int]] = set()
    out: list[CallSite] = []
    for site in sites:
        key = (site.package, site.qualified_name, site.file_path, site.line_number)
        if key in seen:
            continue
        seen.add(key)
        out.append(site)
    return out
