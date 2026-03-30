"""Python dependency diff parsers.

Handles requirements.txt, pyproject.toml, setup.py, and setup.cfg files.
Parses version transitions from unified diff patches (the +/- lines).
"""

from __future__ import annotations

import logging
import re
from fnmatch import fnmatch

from packaging.requirements import InvalidRequirement, Requirement

from franktheunicorn.data_access.dependencies.parser_base import DependencyDiffParser
from franktheunicorn.data_access.dependencies.types import Ecosystem, VersionTransition

logger = logging.getLogger(__name__)

# Regex for version specifier extraction: captures the first version number
# from a specifier like ">=1.21.0", "==2.28.0", "~=3.0", ">=1.4,<2.0"
_VERSION_RE = re.compile(r"[><=!~]+\s*(\d[\w.*]+)")

# Regex for setup.py version assignments: _minimum_pyarrow_version = "0.15.1"
_VERSION_ASSIGN_RE = re.compile(
    r"""_?(?:minimum_|min_|max_)?  # optional prefix
    (\w+?)                         # package name (captured)
    _version\s*=\s*               # _version =
    ["\']([^"\']+)["\']           # quoted version string (captured)
    """,
    re.VERBOSE,
)

# Regex for install_requires list items: "requests>=2.28.0"
_INSTALL_REQUIRES_ITEM_RE = re.compile(r"""["\']([^"\']+)["\']""")


def _extract_version(specifier: str) -> str | None:
    """Extract the primary version number from a PEP 440 specifier string.

    Examples:
        ">=1.21.0" → "1.21.0"
        "==2.28.0" → "2.28.0"
        ">=1.4,<2.0" → "1.4"
        "" → None
    """
    match = _VERSION_RE.search(specifier)
    return match.group(1) if match else None


def _parse_requirement_line(line: str) -> tuple[str, str | None] | None:
    """Parse a requirement line into (package_name, version_or_none).

    Uses packaging.requirements for robust PEP 508 parsing, with a regex
    fallback for lines that don't parse cleanly.
    """
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("-"):
        return None

    try:
        req = Requirement(line)
        version = _extract_version(str(req.specifier)) if req.specifier else None
        return (req.name.lower(), version)
    except InvalidRequirement:
        pass

    # Fallback: try simple name==version or name>=version
    match = re.match(r"([a-zA-Z0-9_.-]+)\s*([><=!~].*)?", line)
    if match:
        name = match.group(1).lower()
        spec = match.group(2) or ""
        return (name, _extract_version(spec))
    return None


def _pair_transitions(
    removed: dict[str, str | None],
    added: dict[str, str | None],
    ecosystem: Ecosystem,
    source_file: str,
) -> tuple[VersionTransition, ...]:
    """Pair removed and added entries for the same package into transitions."""
    transitions: list[VersionTransition] = []
    all_packages = set(removed.keys()) | set(added.keys())

    for pkg in sorted(all_packages):
        old = removed.get(pkg)
        new = added.get(pkg)

        # Skip if package appears in both but version didn't change
        if pkg in removed and pkg in added and old == new:
            continue

        # Skip if package only appears on one side with no version info
        if pkg not in removed and pkg in added and new is None:
            continue
        if pkg in removed and pkg not in added and old is None:
            continue

        transitions.append(VersionTransition(
            package_name=pkg,
            old_version=old if pkg in removed else None,
            new_version=new if pkg in added else None,
            ecosystem=ecosystem,
            source_file=source_file,
        ))

    return tuple(transitions)


class RequirementsTxtParser(DependencyDiffParser):
    """Parse version transitions from requirements.txt / constraints.txt diffs."""

    ecosystem = Ecosystem.PYTHON

    _FILE_PATTERNS = ("requirements*.txt", "constraints*.txt")

    def matches_file(self, filename: str) -> bool:
        basename = filename.rsplit("/", 1)[-1] if "/" in filename else filename
        return any(fnmatch(basename, pat) for pat in self._FILE_PATTERNS)

    def parse(self, patch: str, filename: str) -> tuple[VersionTransition, ...]:
        removed: dict[str, str | None] = {}
        added: dict[str, str | None] = {}

        for line in patch.splitlines():
            if line.startswith("-") and not line.startswith("---"):
                parsed = _parse_requirement_line(line[1:])
                if parsed:
                    removed[parsed[0]] = parsed[1]
            elif line.startswith("+") and not line.startswith("+++"):
                parsed = _parse_requirement_line(line[1:])
                if parsed:
                    added[parsed[0]] = parsed[1]

        return _pair_transitions(removed, added, self.ecosystem, filename)


class PyprojectTomlParser(DependencyDiffParser):
    """Parse version transitions from pyproject.toml diffs.

    Handles dependency strings in [project.dependencies] and
    [project.optional-dependencies] sections. These appear as quoted
    strings like "requests>=2.28" in TOML arrays.
    """

    ecosystem = Ecosystem.PYTHON

    def matches_file(self, filename: str) -> bool:
        basename = filename.rsplit("/", 1)[-1] if "/" in filename else filename
        return basename == "pyproject.toml"

    def parse(self, patch: str, filename: str) -> tuple[VersionTransition, ...]:
        removed: dict[str, str | None] = {}
        added: dict[str, str | None] = {}

        for line in patch.splitlines():
            if line.startswith("-") and not line.startswith("---"):
                self._extract_deps(line[1:], removed)
            elif line.startswith("+") and not line.startswith("+++"):
                self._extract_deps(line[1:], added)

        return _pair_transitions(removed, added, self.ecosystem, filename)

    def _extract_deps(self, line: str, target: dict[str, str | None]) -> None:
        """Extract dependency specs from a TOML line."""
        # Match quoted requirement strings: "requests>=2.28"
        for match in _INSTALL_REQUIRES_ITEM_RE.finditer(line):
            raw = match.group(1)
            parsed = _parse_requirement_line(raw)
            if parsed:
                target[parsed[0]] = parsed[1]


class SetupPyParser(DependencyDiffParser):
    """Parse version transitions from setup.py and setup.cfg diffs.

    Handles two patterns:
    1. Variable assignments: _minimum_pyarrow_version = "0.15.1"
    2. install_requires list items: "requests>=2.28.0"
    """

    ecosystem = Ecosystem.PYTHON

    _FILE_NAMES = ("setup.py", "setup.cfg")

    def matches_file(self, filename: str) -> bool:
        basename = filename.rsplit("/", 1)[-1] if "/" in filename else filename
        return basename in self._FILE_NAMES

    def parse(self, patch: str, filename: str) -> tuple[VersionTransition, ...]:
        removed: dict[str, str | None] = {}
        added: dict[str, str | None] = {}

        for line in patch.splitlines():
            if line.startswith("-") and not line.startswith("---"):
                self._extract_from_line(line[1:], removed)
            elif line.startswith("+") and not line.startswith("+++"):
                self._extract_from_line(line[1:], added)

        return _pair_transitions(removed, added, self.ecosystem, filename)

    def _extract_from_line(self, line: str, target: dict[str, str | None]) -> None:
        """Extract dependency info from a setup.py line."""
        stripped = line.strip()

        # Pattern 1: _minimum_pyarrow_version = "0.15.1"
        match = _VERSION_ASSIGN_RE.search(stripped)
        if match:
            pkg_name = match.group(1).lower().replace("_", "-")
            version = match.group(2)
            target[pkg_name] = version
            return

        # Pattern 2: "requests>=2.28.0" in install_requires list
        for item_match in _INSTALL_REQUIRES_ITEM_RE.finditer(stripped):
            raw = item_match.group(1)
            parsed = _parse_requirement_line(raw)
            if parsed:
                target[parsed[0]] = parsed[1]
