"""Pluggable import resolvers for the review context builder.

A resolver maps a source file to the set of *first-party* files it imports —
modules whose top-level package matches one of the project's package roots.
External and stdlib imports are excluded.

A resolver is registered per file extension. Files with no registered resolver
still get full-file context, just no import expansion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from franktheunicorn.review.import_resolvers.python import PythonImportResolver


class ImportResolver(Protocol):
    """Resolves first-party imports of a single source file."""

    def resolve(
        self,
        file_path: Path,
        repo_path: Path,
        package_roots: list[str],
    ) -> list[Path]:
        """Return absolute paths of first-party imported files."""
        ...


_RESOLVERS: dict[str, ImportResolver] = {
    ".py": PythonImportResolver(),
}


def register_resolver(extension: str, resolver: ImportResolver) -> None:
    """Register a resolver for an extension. Existing entries are overwritten."""
    _RESOLVERS[extension.lower()] = resolver


def get_resolver(file_path: Path | str) -> ImportResolver | None:
    """Return the resolver for ``file_path``'s extension, or None."""
    suffix = Path(file_path).suffix.lower()
    return _RESOLVERS.get(suffix)


__all__ = ["ImportResolver", "PythonImportResolver", "get_resolver", "register_resolver"]
