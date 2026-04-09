"""CVE file history fetcher for scoring.

Scans local git history for commits likely to be CVE/security fixes,
then extracts their changed file paths. Detection strategy adapts
based on project governance:

- ASF projects: terse-commit heuristic (no JIRA link + short message)
- Other projects: git log grep for CVE-YYYY-NNNNN identifiers

Build/dependency files are excluded since they get CVE-related changes
for dependency version bumps but aren't the actual vulnerability source.

Results are cached per repo for CACHE_TTL_SECONDS since CVE history
changes slowly.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CVE_PATTERN = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)

# Build/dependency files to exclude — these get CVE-related changes for
# dependency version bumps but aren't the actual vulnerability source.
BUILD_FILES = frozenset(
    {
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "build.sbt",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "Gemfile",
        "Gemfile.lock",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "composer.json",
        "composer.lock",
        "CMakeLists.txt",
        "dev-requirements.txt",
        "constraints.txt",
    }
)
BUILD_EXTENSIONS = frozenset({".lock", ".gradle", ".sbt"})

# Issue/ticket link patterns that normal (non-security) commits contain.
ISSUE_LINK_PATTERNS = [
    re.compile(r"\[[\w]+-\d+\]"),  # [SPARK-12345], [HADOOP-456]
    re.compile(r"#\d+"),  # GitHub #123
    re.compile(r"https?://\S+/(issues|pull)/\d+"),  # Full URLs
    re.compile(r"\b[A-Z]{2,}-\d+\b"),  # Bare JIRA IDs: SPARK-123
]

# Commits matching these patterns are not security fixes.
SKIP_PATTERNS = [
    re.compile(r"^Merge\b", re.IGNORECASE),
    re.compile(r"^Revert\b", re.IGNORECASE),
    re.compile(r"\b(release|version|bump)\b", re.IGNORECASE),
    re.compile(r"^Preparing\b", re.IGNORECASE),
]

TERSE_MESSAGE_MAX_LENGTH = 120  # chars
CACHE_TTL_SECONDS = 86400  # 24 hours

_cache: dict[str, tuple[float, set[str]]] = {}


def _is_build_file(path: str) -> bool:
    """Check if a file is a build/dependency file (excluded from CVE results)."""
    name = Path(path).name
    if name in BUILD_FILES:
        return True
    suffix = Path(path).suffix.lower()
    return suffix in BUILD_EXTENSIONS


def _has_issue_link(message: str) -> bool:
    """Check if a commit message contains any issue/ticket reference."""
    return any(p.search(message) for p in ISSUE_LINK_PATTERNS)


def _is_skip_commit(message: str) -> bool:
    """Check if a commit message matches a skip pattern (merge, revert, etc.)."""
    return any(p.search(message) for p in SKIP_PATTERNS)


def _scan_cve_grep(repo_path: Path) -> set[str]:
    """Scan git log for commits mentioning CVE identifiers.

    Used for standard/personal/corporate governance projects that openly
    reference CVEs in commit messages.

    Returns set of file paths from CVE-mentioning commits.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--all",
                "--grep=CVE-",
                "--name-only",
                "--format=%x00%s",
            ],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug("git log --grep=CVE- failed: %s", result.stderr[:200])
            return set()
    except subprocess.TimeoutExpired:
        logger.warning("git log --grep=CVE- timed out for %s", repo_path)
        return set()
    except Exception:
        logger.debug("Error scanning CVE commits in %s", repo_path, exc_info=True)
        return set()

    files: set[str] = set()
    current_is_cve = False

    for line in result.stdout.splitlines():
        if line.startswith("\x00"):
            # Header line: NUL + subject
            subject = line[1:]
            current_is_cve = bool(CVE_PATTERN.search(subject))
        elif line.strip() and current_is_cve:
            # File path line belonging to a CVE commit.
            if not _is_build_file(line.strip()):
                files.add(line.strip())

    return files


def _scan_terse_commits(repo_path: Path) -> set[str]:
    """Scan git log for terse commits with no issue link.

    Used for ASF governance projects where CVE fixes are deliberately
    committed without JIRA links and with minimal messages.

    Returns set of file paths from suspicious terse commits.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--all",
                "--after=5 years ago",
                "--name-only",
                "--format=%x00%s",
            ],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=60,
        )
        if result.returncode != 0:
            logger.debug("git log for terse scan failed: %s", result.stderr[:200])
            return set()
    except subprocess.TimeoutExpired:
        logger.warning("git log terse scan timed out for %s", repo_path)
        return set()
    except Exception:
        logger.debug("Error scanning terse commits in %s", repo_path, exc_info=True)
        return set()

    files: set[str] = set()
    current_is_terse = False

    for line in result.stdout.splitlines():
        if line.startswith("\x00"):
            # Header line: NUL + subject
            subject = line[1:]
            current_is_terse = (
                bool(subject)
                and len(subject) <= TERSE_MESSAGE_MAX_LENGTH
                and not _has_issue_link(subject)
                and not _is_skip_commit(subject)
            )
        elif line.strip() and current_is_terse:
            if not _is_build_file(line.strip()):
                files.add(line.strip())

    return files


def fetch_cve_affected_files(
    repo_path: Path,
    governance: str = "standard",
    extra_cve_files: list[str] | None = None,
) -> list[str]:
    """Return sorted list of file paths likely involved in CVE/security fixes.

    Detection strategy auto-switches based on governance:
    - "asf": terse-commit heuristic
    - other: git log CVE grep

    Results are merged with extra_cve_files (from project config)
    and cached per repo + governance for CACHE_TTL_SECONDS.
    """
    cache_key = f"{repo_path}:{governance}"
    now = time.monotonic()

    if cache_key in _cache:
        cached_time, cached_files = _cache[cache_key]
        if now - cached_time < CACHE_TTL_SECONDS:
            merged = cached_files | set(extra_cve_files or [])
            return sorted(merged)

    scanned = _scan_terse_commits(repo_path) if governance == "asf" else _scan_cve_grep(repo_path)

    _cache[cache_key] = (now, scanned)

    merged = scanned | set(extra_cve_files or [])
    return sorted(merged)
