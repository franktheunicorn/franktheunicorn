"""Typed dataclasses for dependency change tracking (immutable DTOs)."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from franktheunicorn.data_access.base import FetchResult


class Ecosystem(enum.StrEnum):
    """Package ecosystem identifier."""

    PYTHON = "python"
    JAVA = "java"  # future
    RUST = "rust"  # future


@dataclass(frozen=True)
class VersionTransition:
    """A single dependency version change detected in a diff."""

    package_name: str
    old_version: str | None  # None = newly added dependency
    new_version: str | None  # None = removed dependency
    ecosystem: Ecosystem
    source_file: str  # e.g. "requirements.txt", "python/setup.py"


@dataclass(frozen=True)
class DependencyDiff:
    """All version transitions found across dependency files in a PR diff."""

    transitions: tuple[VersionTransition, ...] = ()
    source_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangelogEntry(FetchResult):
    """Release notes / changelog for a dependency version transition."""

    package_name: str = ""
    old_version: str = ""
    new_version: str = ""
    release_notes: str = ""  # Markdown/text of release notes
    changelog_url: str = ""  # Link for the reviewer to click through
    repository_url: str = ""  # Source repo URL (from PyPI metadata etc.)
    release_date: str = ""
    breaking_changes_detected: bool = False
    deprecations_detected: bool = False
    fetch_error: str = ""  # Non-fatal error description (e.g. "no GitHub releases found")


# Keys to search in PyPI project_urls for source repo, in priority order (case-insensitive).
_SOURCE_URL_KEYS: tuple[str, ...] = (
    "source",
    "source code",
    "repository",
    "code",
    "homepage",
    "home",
)

# Keys to search in PyPI project_urls for changelog URL (case-insensitive).
_CHANGELOG_URL_KEYS: tuple[str, ...] = (
    "changelog",
    "changes",
    "release notes",
    "what's new",
    "history",
)

# Keywords that suggest breaking changes in release notes.
# Note: "removed" alone is too broad (false positives on "Removed unused import").
_BREAKING_KEYWORDS: tuple[str, ...] = (
    "breaking",
    "backward incompatible",
    "backwards incompatible",
    "no longer supported",
)

# Keywords that suggest deprecations in release notes.
# "deprecat" matches "deprecated", "deprecation", "deprecating" via substring.
_DEPRECATION_KEYWORDS: tuple[str, ...] = ("deprecat",)


def detect_breaking_changes(text: str) -> bool:
    """Return True if the text likely mentions breaking changes."""
    lower = text.lower()
    return any(kw in lower for kw in _BREAKING_KEYWORDS)


def detect_deprecations(text: str) -> bool:
    """Return True if the text likely mentions deprecations."""
    lower = text.lower()
    return any(kw in lower for kw in _DEPRECATION_KEYWORDS)


def extract_github_owner_repo(url: str) -> tuple[str, str] | None:
    """Parse owner/repo from a GitHub URL, stripping /tree/... suffixes.

    Returns (owner, repo) or None if the URL isn't a GitHub repo URL.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if "github.com" not in (parsed.hostname or ""):
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    repo = parts[1].removesuffix(".git")
    return (parts[0], repo)


def _find_url_by_keys(
    project_urls: dict[str, str] | None,
    keys: tuple[str, ...],
    *,
    require_github: bool = False,
) -> str:
    """Search project_urls case-insensitively by priority keys."""
    if not project_urls:
        return ""
    lowered = {k.lower(): v for k, v in project_urls.items()}
    for key in keys:
        url = lowered.get(key, "")
        if url and (not require_github or "github.com" in url):
            return url
    return ""


def find_source_url(
    project_urls: dict[str, str] | None,
    home_page: str | None = None,
) -> str:
    """Find the source repository URL from PyPI project metadata."""
    url = _find_url_by_keys(project_urls, _SOURCE_URL_KEYS, require_github=True)
    if url:
        return url
    if home_page and "github.com" in home_page:
        return home_page
    return ""


def find_changelog_url(project_urls: dict[str, str] | None) -> str:
    """Find a direct changelog URL from PyPI project metadata."""
    return _find_url_by_keys(project_urls, _CHANGELOG_URL_KEYS)


def version_to_tag_candidates(
    version: str,
    package_name: str = "",
    repo_name: str = "",
) -> list[str]:
    """Generate GitHub release tag candidates in order of likelihood.

    Based on research across 14 major Python packages:
    - ~50-60% use v{version} (requests, numpy, pandas, pydantic)
    - ~25-30% use bare {version} (flask, httpx, boto3, packaging)
    - Some use exotic patterns (sqlalchemy: rel_2_0_48)
    - Packages like pytest and fastapi changed conventions mid-life
    - Monorepo packages may prefix with package or repo name
    """
    candidates = [
        f"v{version}",  # most common
        version,  # second most common
        f"release-{version}",
        f"rel_{version.replace('.', '_')}",  # sqlalchemy
    ]
    if package_name:
        candidates.extend(
            [
                f"{package_name}-v{version}",  # monorepo (azure-sdk)
                f"{package_name}-{version}",
            ]
        )
    if repo_name and repo_name != package_name:
        candidates.extend(
            [
                f"{repo_name}-{version}",  # pyarrow → apache-arrow-1.0.0
                f"{repo_name}-v{version}",
            ]
        )
    return list(dict.fromkeys(candidates))
