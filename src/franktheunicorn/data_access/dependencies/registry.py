"""Registry mapping dependency filenames to parsers.

Provides a quick ``is_dependency_file`` check (used to gate expensive diff
fetches) and a ``parse_dependency_changes`` function that runs all matching
parsers over a set of PR file changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.data_access.dependencies.parser_base import DependencyDiffParser
from franktheunicorn.data_access.dependencies.python_parsers import (
    PyprojectTomlParser,
    RequirementsTxtParser,
    SetupPyParser,
)
from franktheunicorn.data_access.dependencies.types import (
    DependencyDiff,
    VersionTransition,
)

if TYPE_CHECKING:
    from franktheunicorn.data_access.github.types import PRFileChange

# Registered parsers — order doesn't matter since each checks matches_file.
# Extend this list when adding Java (MavenPomParser, GradleBuildParser)
# or Rust (CargoTomlParser) support.
_PARSERS: list[DependencyDiffParser] = [
    RequirementsTxtParser(),
    PyprojectTomlParser(),
    SetupPyParser(),
]


def get_parser_for_file(filename: str) -> DependencyDiffParser | None:
    """Return the first parser that matches the given filename, or None."""
    for parser in _PARSERS:
        if parser.matches_file(filename):
            return parser
    return None


def is_dependency_file(filename: str) -> bool:
    """Quick check: does any registered parser handle this filename?

    Used to gate expensive diff fetches — only fetch the full diff when
    the PR touches at least one dependency file.
    """
    return get_parser_for_file(filename) is not None


def parse_dependency_changes(files: tuple[PRFileChange, ...]) -> DependencyDiff:
    """Parse all dependency files in a PR diff, returning combined transitions.

    Iterates over the file changes, finds matching parsers, and collects
    all version transitions into a single ``DependencyDiff``.
    """
    all_transitions: list[VersionTransition] = []
    source_files: list[str] = []

    for file_change in files:
        parser = get_parser_for_file(file_change.filename)
        if parser is None or not file_change.patch:
            continue

        transitions = parser.parse(file_change.patch, file_change.filename)
        if transitions:
            all_transitions.extend(transitions)
            source_files.append(file_change.filename)

    return DependencyDiff(
        transitions=tuple(all_transitions),
        source_files=tuple(sorted(set(source_files))),
    )
