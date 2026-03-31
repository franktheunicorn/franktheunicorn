"""Local git blame data fetcher for scoring (v1.25).

Runs `git blame --porcelain` on changed files to extract per-line author
information. Returns data in the format expected by score_touches_operator_code().

Design doc: "Run git blame on the base branch for changed files each time.
No blame cache."
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Max files to blame per PR (design doc: skip blame for PRs > 50 files).
MAX_BLAME_FILES = 50

# Proximity window: lines within this range of changed lines count as "near".
NEAR_LINES_WINDOW = 5

# Extensions to skip (docs, configs, generated).
SKIP_EXTENSIONS = frozenset(
    {
        ".md",
        ".rst",
        ".txt",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".cfg",
        ".ini",
        ".lock",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
    }
)


@dataclass
class BlameEntry:
    """Blame data for a single file, matching scorer's expected format."""

    file_path: str
    authors: list[str] = field(default_factory=list)
    near_authors: list[str] = field(default_factory=list)


def _is_code_file(path: str) -> bool:
    """Check if a file is a code file worth blaming."""
    suffix = Path(path).suffix.lower()
    return suffix not in SKIP_EXTENSIONS


def _parse_porcelain_blame(output: str) -> dict[int, str]:
    """Parse `git blame --porcelain` output into {line_number: author} mapping.

    Porcelain format: each blame entry starts with a header line
    ``<sha> <orig_line> <final_line> [<num_lines>]``. The first time a commit
    appears, full metadata follows (including ``author <name>``). Subsequent
    appearances only show the header + the source line. We track authors by
    commit SHA to handle this.
    """
    authors: dict[int, str] = {}
    commit_authors: dict[str, str] = {}
    current_sha = ""
    current_line = 0

    for line in output.splitlines():
        match = re.match(r"^([0-9a-f]{40})\s+\d+\s+(\d+)", line)
        if match:
            current_sha = match.group(1)
            current_line = int(match.group(2))
        elif line.startswith("author "):
            author_name = line[7:].strip()
            if current_sha:
                commit_authors[current_sha] = author_name
        elif line.startswith("\t"):
            # Source line — associate with the current commit's author.
            if current_line > 0 and current_sha:
                author = commit_authors.get(current_sha, "")
                if author:
                    authors[current_line] = author
            current_line = 0

    return authors


def fetch_blame_for_file(
    repo_path: Path,
    file_path: str,
    base_ref: str = "HEAD",
) -> dict[int, str] | None:
    """Run git blame for a single file and return {line: author} mapping.

    Returns None if the file doesn't exist or blame fails.
    """
    try:
        result = subprocess.run(
            ["git", "blame", "--porcelain", base_ref, "--", file_path],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug("git blame failed for %s: %s", file_path, result.stderr[:200])
            return None
        return _parse_porcelain_blame(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("git blame timed out for %s", file_path)
        return None
    except Exception:
        logger.debug("Error running git blame for %s", file_path, exc_info=True)
        return None


def fetch_blame_for_files(
    repo_path: Path,
    changed_files: list[str],
    base_ref: str = "HEAD",
) -> list[dict[str, object]]:
    """Fetch blame data for changed files, returning scorer-compatible format.

    Returns list of dicts with keys: file_path, authors, near_authors.
    This matches the format expected by score_touches_operator_code() in
    scoring/blame.py.

    Caps at MAX_BLAME_FILES. Skips non-code files.
    """
    code_files = [f for f in changed_files if _is_code_file(f)]

    if len(code_files) > MAX_BLAME_FILES:
        logger.info(
            "PR touches %d code files; capping blame at %d",
            len(code_files),
            MAX_BLAME_FILES,
        )
        code_files = code_files[:MAX_BLAME_FILES]

    results: list[dict[str, object]] = []

    for file_path in code_files:
        blame = fetch_blame_for_file(repo_path, file_path, base_ref)
        if blame is None:
            continue

        # All authors who authored any line in the file.
        all_authors = list(set(blame.values()))

        # "Near authors" — authors of lines adjacent to the code.
        # Since we don't have diff hunk info here, we treat all authors as
        # both direct and near. The scorer uses this for proximity scoring.
        results.append(
            {
                "file_path": file_path,
                "authors": all_authors,
                "near_authors": all_authors,
            }
        )

    return results
