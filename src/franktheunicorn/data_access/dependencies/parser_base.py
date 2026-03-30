"""Abstract base class for dependency diff parsers.

Each ecosystem (Python, Java, Rust) provides concrete parsers that know how to
extract version transitions from unified diff patches of dependency files.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from franktheunicorn.data_access.dependencies.types import Ecosystem, VersionTransition


class DependencyDiffParser(ABC):
    """Parse dependency version transitions from a unified diff patch.

    Subclasses implement parsing for specific file formats (requirements.txt,
    pyproject.toml, pom.xml, Cargo.toml, etc.) and declare which filenames
    they handle via ``matches_file``.
    """

    ecosystem: Ecosystem

    @abstractmethod
    def parse(self, patch: str, filename: str) -> tuple[VersionTransition, ...]:
        """Extract version transitions from a unified diff patch.

        Args:
            patch: The unified diff patch text (the ``+``/``-`` lines from a
                single file's diff section).
            filename: The filename that was changed (e.g. ``requirements.txt``).

        Returns:
            Tuple of version transitions found in the patch.
        """

    @abstractmethod
    def matches_file(self, filename: str) -> bool:
        """Return True if this parser handles the given filename."""
