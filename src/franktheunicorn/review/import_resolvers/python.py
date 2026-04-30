"""Python import resolver — AST-based first-party import extraction."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class PythonImportResolver:
    """Resolves first-party imports in a Python source file.

    "First-party" means the module's top-level package matches one of the
    configured ``package_roots``. Imports of stdlib or third-party packages
    are filtered out.

    Resolution strategy: for each ``import X.Y`` or ``from X.Y import Z``,
    treat ``X`` as the top-level package. If ``X`` is in ``package_roots``,
    look for the module file under common layouts:
      - ``repo_path/X/Y.py``
      - ``repo_path/X/Y/__init__.py``
      - ``repo_path/src/X/Y.py``
      - ``repo_path/src/X/Y/__init__.py``
    Returns existing files only; unresolved imports are silently skipped.
    """

    def resolve(
        self,
        file_path: Path,
        repo_path: Path,
        package_roots: list[str],
    ) -> list[Path]:
        if not package_roots:
            return []
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        try:
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError:
            return []

        package_root_set = {root.strip(".") for root in package_roots if root.strip(".")}
        modules: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    # Relative import — treated as first-party by definition.
                    rel_module = self._resolve_relative_module(file_path, repo_path, node)
                    if rel_module:
                        modules.add(rel_module)
                elif node.module:
                    modules.add(node.module)
                    for alias in node.names:
                        modules.add(f"{node.module}.{alias.name}")

        first_party_modules = {mod for mod in modules if mod.split(".", 1)[0] in package_root_set}

        resolved: list[Path] = []
        seen: set[Path] = set()
        for mod in sorted(first_party_modules):
            for candidate in self._candidate_paths(repo_path, mod):
                if candidate.is_file() and candidate not in seen:
                    seen.add(candidate)
                    resolved.append(candidate)
                    break

        return resolved

    @staticmethod
    def _candidate_paths(repo_path: Path, dotted: str) -> list[Path]:
        parts = dotted.split(".")
        as_file = Path(*parts).with_suffix(".py")
        as_pkg = Path(*parts) / "__init__.py"
        return [
            repo_path / as_file,
            repo_path / as_pkg,
            repo_path / "src" / as_file,
            repo_path / "src" / as_pkg,
        ]

    @staticmethod
    def _resolve_relative_module(
        file_path: Path,
        repo_path: Path,
        node: ast.ImportFrom,
    ) -> str | None:
        """Convert a relative import (e.g. ``from ..foo import bar``) to dotted form.

        Walks up ``node.level`` directories from ``file_path`` and joins with
        ``node.module``. Returns the dotted path relative to ``repo_path`` (or
        ``repo_path/src``) — that lets the top-level component pass the
        package_root filter.
        """
        try:
            rel = file_path.resolve().relative_to(repo_path.resolve())
        except ValueError:
            return None

        parts = list(rel.parts[:-1])  # drop the filename
        # Strip a leading "src" so the top-level package matches package_roots.
        if parts and parts[0] == "src":
            parts = parts[1:]

        # Walk up `level - 1` (level=1 means current package).
        ups = node.level - 1
        if ups > len(parts):
            return None
        parts = parts[: len(parts) - ups] if ups else parts

        if node.module:
            parts.append(node.module)
        if not parts:
            return None
        return ".".join(parts)


__all__ = ["PythonImportResolver"]
