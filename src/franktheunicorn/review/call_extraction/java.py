"""Extract Java call sites from a unified diff (regex-based).

Tree-sitter would give a more accurate parse, but regex is cheap and good
enough for the api-misuse first pass: we look at ``import`` lines in the
hunk, map each imported simple name to its FQCN, and then scan added
lines for ``Identifier.method(`` invocations whose head matches an
imported class. Stdlib (``java.*``, ``javax.*``) and the project's own
group/artifact prefix are filtered out.

Maven coordinates (groupId:artifactId) are not present in source code,
so the package field is set to the FQCN's top-level segments
(e.g. ``com.google.guava``). The package_registry resolver is
responsible for mapping that to a Maven coordinate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from unidiff import PatchSet  # type: ignore[import-untyped]

from franktheunicorn.review.call_extraction.types import CallSite, Language

logger = logging.getLogger(__name__)

_STDLIB_PREFIXES: tuple[str, ...] = ("java.", "javax.", "jdk.", "sun.", "com.sun.")

_IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w\.]+(?:\.\*)?)\s*;\s*$")
_CALL_RE = re.compile(r"\b([A-Z][\w]*)\.(\w+)\s*\(")


@dataclass(frozen=True)
class _JavaImport:
    """Imported FQCN and the simple name it binds locally."""

    fqcn: str
    simple: str


def extract_java_calls(diff: str, *, project_packages: list[str] | None = None) -> list[CallSite]:
    """Return external Java call sites observed in the diff."""
    try:
        patch = PatchSet(diff)
    except Exception:
        logger.debug("Failed to parse diff as PatchSet", exc_info=True)
        return []

    roots = tuple(project_packages or [])
    sites: list[CallSite] = []
    for pf in patch:
        path = getattr(pf, "path", "") or getattr(pf, "target_file", "")
        if not path.endswith(".java"):
            continue
        for hunk in pf:
            sites.extend(_extract_from_hunk(path, hunk, roots))
    return _dedupe(sites)


def _extract_from_hunk(
    path: str, hunk: object, project_packages: tuple[str, ...]
) -> list[CallSite]:
    added: list[tuple[int, str]] = []
    for line in hunk:  # type: ignore[attr-defined]
        if line.is_added:
            added.append((line.target_line_no, line.value.rstrip("\n")))

    if not added:
        return []

    imports = _collect_imports(added)
    sites: list[CallSite] = []

    for line_no, source in added:
        snippet = source.strip()
        for match in _CALL_RE.finditer(source):
            simple, method = match.group(1), match.group(2)
            imp = imports.get(simple)
            if imp is None:
                continue
            if _should_skip(imp.fqcn, project_packages):
                continue
            top_pkg = _top_package(imp.fqcn)
            qualified = f"{imp.fqcn}.{method}"
            sites.append(
                CallSite(
                    language=Language.JAVA,
                    package=top_pkg,
                    qualified_name=qualified,
                    file_path=path,
                    line_number=line_no,
                    snippet=snippet,
                )
            )
    return sites


def _collect_imports(added: list[tuple[int, str]]) -> dict[str, _JavaImport]:
    bindings: dict[str, _JavaImport] = {}
    for _, source in added:
        match = _IMPORT_RE.match(source)
        if not match:
            continue
        fqcn = match.group(2)
        if fqcn.endswith(".*"):
            # Wildcard imports give us no simple-name → FQCN mapping; skip.
            continue
        simple = fqcn.rsplit(".", 1)[-1]
        bindings[simple] = _JavaImport(fqcn=fqcn, simple=simple)
    return bindings


def _top_package(fqcn: str) -> str:
    """Return the top three dotted segments of an FQCN as a coarse package id."""
    parts = fqcn.split(".")
    if len(parts) <= 3:
        return ".".join(parts[:-1]) if len(parts) > 1 else fqcn
    return ".".join(parts[:3])


def _should_skip(fqcn: str, project_packages: tuple[str, ...]) -> bool:
    if not fqcn:
        return True
    if fqcn.startswith(_STDLIB_PREFIXES):
        return True
    return any(fqcn == root or fqcn.startswith(root + ".") for root in project_packages)


def _dedupe(sites: list[CallSite]) -> list[CallSite]:
    seen: set[tuple[str, str, str, int]] = set()
    out: list[CallSite] = []
    for site in sites:
        key = (site.package, site.qualified_name, site.file_path, site.line_number)
        if key in seen:
            continue
        seen.add(key)
        out.append(site)
    return out
