"""Dependency changelog fetching — detect version changes in PRs and fetch release notes."""

from franktheunicorn.data_access.dependencies.changelog_fetcher import (
    ChangelogFetcher,
    PythonChangelogFetcher,
)
from franktheunicorn.data_access.dependencies.parser_base import DependencyDiffParser
from franktheunicorn.data_access.dependencies.python_parsers import (
    PyprojectTomlParser,
    RequirementsTxtParser,
    SetupPyParser,
)
from franktheunicorn.data_access.dependencies.registry import (
    get_parser_for_file,
    is_dependency_file,
    parse_dependency_changes,
)
from franktheunicorn.data_access.dependencies.types import (
    ChangelogEntry,
    DependencyDiff,
    Ecosystem,
    VersionTransition,
)

__all__ = [
    "ChangelogEntry",
    "ChangelogFetcher",
    "DependencyDiff",
    "DependencyDiffParser",
    "Ecosystem",
    "PyprojectTomlParser",
    "PythonChangelogFetcher",
    "RequirementsTxtParser",
    "SetupPyParser",
    "VersionTransition",
    "get_parser_for_file",
    "is_dependency_file",
    "parse_dependency_changes",
]
