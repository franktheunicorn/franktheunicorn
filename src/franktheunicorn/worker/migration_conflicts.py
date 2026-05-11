"""Detect Django migration conflicts in a checked-out target repo.

Pure functions — no Django imports. Operates on an on-disk repo path so the
worker can run this against any project being reviewed without needing to
boot the target's settings module.

Two pieces:
- ``is_django_project(repo_path)``: filesystem-level detection so the check
  is a no-op on non-Django repos.
- ``detect_migration_conflicts(repo_path)``: parses every migration file via
  ``ast`` (no code execution), reconstructs the per-app dependency graph and
  returns the leaf-node conflicts that would block ``manage.py migrate``.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Directories never worth descending into when looking for Django apps.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "site-packages",
        "build",
        "dist",
        ".eggs",
    }
)

# Bound the project-shape walk; deep monorepos can have very large trees and
# Django apps live near the top in every layout we care about (root, src/,
# backend/, server/, etc.).
_MAX_WALK_DEPTH = 5

# Cap how much of ``manage.py`` we read when sniffing for the Django marker.
# Real Django-generated manage.py files are well under 1 KiB; this is a hedge
# against pathological files (e.g. a binary blob renamed to manage.py).
_MANAGE_PY_MAX_BYTES = 32 * 1024


@dataclass(frozen=True)
class MigrationConflict:
    """Two or more leaf migrations in the same app — ``migrate`` will refuse."""

    app_label: str
    leaf_migrations: tuple[str, ...]


@dataclass
class MigrationConflictReport:
    """Result of a migration conflict scan."""

    is_django_project: bool = False
    conflicts: list[MigrationConflict] = field(default_factory=list)
    apps_scanned: int = 0
    migrations_scanned: int = 0


def is_django_project(repo_path: Path) -> bool:
    """Return True if ``repo_path`` looks like a Django project on disk.

    Recognises two signals:

    1. A top-level ``manage.py`` whose contents reference Django. The
       filename alone is not enough — a few unrelated tools (build helpers,
       legacy scripts) also ship a ``manage.py``, and the canonical
       Django-generated file always imports from ``django.core.management``
       or sets ``DJANGO_SETTINGS_MODULE``, so a case-insensitive substring
       match for ``django`` is a reliable disambiguator.
    2. Any ``<app>/migrations/__init__.py`` reachable within
       ``_MAX_WALK_DEPTH`` levels (so library-style apps without a
       ``manage.py`` are still caught).
    """
    if not repo_path.is_dir():
        return False
    if _manage_py_mentions_django(repo_path / "manage.py"):
        return True
    for _ in _iter_migration_dirs(repo_path):
        return True
    return False


def _manage_py_mentions_django(path: Path) -> bool:
    """Return True if ``path`` is a regular file whose contents mention Django."""
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            blob = fh.read(_MANAGE_PY_MAX_BYTES)
    except OSError as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return False
    return b"django" in blob.lower()


def detect_migration_conflicts(repo_path: Path) -> MigrationConflictReport:
    """Scan a target repo for leaf-node migration conflicts.

    Returns an empty report (``is_django_project=False``) if the path isn't a
    Django project. Otherwise, walks every ``<app>/migrations/`` directory,
    parses each migration via ``ast``, builds the per-app dependency graph,
    and reports apps with more than one leaf migration.
    """
    report = MigrationConflictReport()
    if not is_django_project(repo_path):
        return report
    report.is_django_project = True

    for migrations_dir in _iter_migration_dirs(repo_path):
        app_label = migrations_dir.parent.name
        migration_names: list[str] = []
        children: dict[str, set[str]] = {}

        for migration_file in sorted(migrations_dir.glob("*.py")):
            if migration_file.name == "__init__.py":
                continue
            name = migration_file.stem
            deps = _parse_migration_dependencies(migration_file)
            if deps is None:
                continue
            migration_names.append(name)
            children.setdefault(name, set())
            for dep_app, dep_name in deps:
                if dep_app == app_label:
                    children.setdefault(dep_name, set()).add(name)

        if not migration_names:
            continue
        report.apps_scanned += 1
        report.migrations_scanned += len(migration_names)

        leaves = sorted(name for name in migration_names if not children.get(name))
        if len(leaves) > 1:
            report.conflicts.append(
                MigrationConflict(app_label=app_label, leaf_migrations=tuple(leaves))
            )

    report.conflicts.sort(key=lambda c: c.app_label)
    return report


def _iter_migration_dirs(repo_path: Path) -> Iterator[Path]:
    """Yield each ``<app>/migrations/`` directory under ``repo_path``.

    A migrations directory must contain ``__init__.py`` to be recognised
    as a Django app's migrations package. The walk skips virtualenvs,
    build outputs, and other directories that frequently contain
    third-party migrations we don't want to flag.
    """
    for app_dir in _walk_limited(repo_path, max_depth=_MAX_WALK_DEPTH):
        candidate = app_dir / "migrations"
        if candidate.is_dir() and (candidate / "__init__.py").is_file():
            yield candidate


def _walk_limited(root: Path, *, max_depth: int) -> Iterator[Path]:
    """Yield directories under ``root`` up to ``max_depth`` levels deep.

    Symlinks are skipped — target repos are untrusted, and a symlink
    pointing outside the checkout could escape the bounded walk or trip
    the per-app migration parser on unrelated files.
    """
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for child in entries:
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name in _SKIP_DIRS or child.name.startswith("."):
                continue
            yield child
            stack.append((child, depth + 1))


def _parse_migration_dependencies(file_path: Path) -> list[tuple[str, str]] | None:
    """Return the ``dependencies`` list of a migration file.

    - Returns the parsed list (possibly empty) when a ``class Migration`` is
      found, mirroring Django's own default of ``dependencies = []`` when
      the attribute is omitted.
    - Returns ``None`` when the file is unreadable, has a syntax error, or
      contains no ``class Migration`` — those files don't contribute to the
      migration graph and shouldn't be counted as scanned.

    Uses ``ast`` so we never execute the migration module, which is important
    when scanning untrusted target repos.
    """
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError, ValueError) as exc:
        logger.debug("Failed to parse migration %s: %s", file_path, exc)
        return None

    found_migration_class = False
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == "Migration"):
            continue
        found_migration_class = True
        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            for target in item.targets:
                if isinstance(target, ast.Name) and target.id == "dependencies":
                    return _eval_dependencies(item.value)
    return [] if found_migration_class else None


def _eval_dependencies(node: ast.AST) -> list[tuple[str, str]]:
    """Extract a list of ``(app_label, migration_name)`` tuples from an AST node."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    deps: list[tuple[str, str]] = []
    for elt in node.elts:
        if not isinstance(elt, ast.Tuple) or len(elt.elts) != 2:
            continue
        app = _const_str(elt.elts[0])
        name = _const_str(elt.elts[1])
        if app and name:
            deps.append((app, name))
    return deps


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
