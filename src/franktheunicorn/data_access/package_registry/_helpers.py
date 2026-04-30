"""Shared helpers for PyPI and Maven docs fetchers.

These were duplicated between ``pypi.py`` and ``maven.py``. Centralising
them keeps the regex patterns, signature unpacking, and presentation in
one place.
"""

from __future__ import annotations

import re

from franktheunicorn.data_access.package_registry.types import PackageDocs

_BIG_O_RE = re.compile(r"\bO\s*\([^)]+\)")
_COMPLEXITY_RE = re.compile(r"(?im)^(?:complexity|time complexity|performance)\s*[:\-].*$")
_DEPRECATED_RE = re.compile(r"(?im)\b(?:deprecated since|\.\.\s*deprecated::|@deprecated)\b.*$")


def extract_complexity(text: str) -> str:
    """Return a ``"; "``-joined string of complexity hints found in ``text``.

    Matches lines that start with "Complexity:" / "Performance:" and any
    big-O expression. Returns the empty string if nothing matches.
    """
    if not text:
        return ""
    parts: list[str] = []
    parts.extend(match.group(0).strip() for match in _COMPLEXITY_RE.finditer(text))
    parts.extend(match.group(0) for match in _BIG_O_RE.finditer(text))
    deduped: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(p)
    return "; ".join(deduped)


def detect_deprecation(text: str) -> tuple[bool, str]:
    """Return ``(is_deprecated, message)`` for a free-form docstring."""
    if not text:
        return False, ""
    match = _DEPRECATED_RE.search(text)
    if match is None:
        return False, ""
    return True, match.group(0).strip()


def truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def unpack_args(args: tuple[object, ...], kwargs: dict[str, object]) -> tuple[str, str]:
    """Resolve ``(package, qualified_name)`` from ``DataFetcher.fetch`` *args/**kwargs."""
    package = str(kwargs.get("package", args[0] if args else ""))
    qualified = str(kwargs.get("qualified_name", args[1] if len(args) > 1 else ""))
    if not package:
        msg = "package is required"
        raise ValueError(msg)
    return package, qualified


def format_docs_block(docs: list[PackageDocs]) -> str:
    """Render a list of :class:`PackageDocs` into a compact text block.

    Used by the api-misuse check's prompt assembly, but kept here so the
    rendering logic lives next to the data type it formats.
    """
    if not docs:
        return "(no upstream docs found for the third-party calls in this PR)"

    parts: list[str] = []
    for d in docs:
        section = [f"- {d.package} :: {d.qualified_name}"]
        if d.version:
            section.append(f"  version: {d.version}")
        if d.signature:
            section.append(f"  signature: {d.signature}")
        if d.deprecated:
            label = d.deprecation_message or "deprecated"
            section.append(f"  deprecated: {label}")
        if d.complexity_notes:
            section.append(f"  complexity: {d.complexity_notes}")
        if d.docstring:
            section.append(f"  docstring: {d.docstring}")
        if d.doc_url:
            section.append(f"  docs: {d.doc_url}")
        parts.append("\n".join(section))
    return "\n\n".join(parts)
