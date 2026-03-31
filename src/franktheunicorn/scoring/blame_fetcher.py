"""Local git blame data fetcher for scoring (v1.25).

Runs `git blame --porcelain` on changed files to extract per-line author
information. Uses `git diff` to determine which lines actually changed,
then classifies blame authors as:
- "authors": who authored the lines being changed (Layer 1 — full credit)
- "near_authors": who authored lines near the changes (Layer 2 — half credit)

Returns data in the format expected by score_touches_operator_code().

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


def _parse_diff_changed_lines(diff_output: str) -> set[int]:
    """Parse unified diff output (with zero context) to extract changed line numbers.

    Expects output from ``git diff -U0`` so hunk headers contain only the
    actually changed lines, not surrounding context.  Extracts the old-side
    (a-side) line ranges — these are the lines being modified/deleted.

    Format: @@ -start,count +start,count @@
    A count of 0 means a pure insertion (no old lines touched).
    """
    changed: set[int] = set()
    for match in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+", diff_output, re.MULTILINE):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        # count=0 means pure insertion — no old lines are changed.
        for line_no in range(start, start + count):
            changed.add(line_no)
    return changed


def _get_changed_lines_for_file(
    repo_path: Path,
    file_path: str,
    base_ref: str,
    head_ref: str | None = None,
) -> set[int] | None:
    """Get the set of line numbers changed in a file between two refs.

    If head_ref is provided, diffs base_ref..head_ref (two explicit commits).
    Otherwise, diffs base_ref against the working tree (less reliable).

    Returns None if diff fails (e.g. new file with no base).
    """
    try:
        diff_spec = f"{base_ref}..{head_ref}" if head_ref else base_ref
        result = subprocess.run(
            ["git", "diff", "-U0", diff_spec, "--", file_path],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=15,
        )
        if result.returncode != 0:
            return None
        return _parse_diff_changed_lines(result.stdout)
    except Exception:
        logger.debug("git diff failed for %s", file_path, exc_info=True)
        return None


def _classify_authors(
    blame: dict[int, str],
    changed_lines: set[int],
    near_window: int = NEAR_LINES_WINDOW,
) -> tuple[set[str], set[str]]:
    """Classify blame authors into direct (changed lines) and near (adjacent).

    Returns (direct_authors, near_only_authors) where near_only excludes
    anyone already in direct_authors.
    """
    direct_authors: set[str] = set()
    near_authors: set[str] = set()

    # Build the "near" set: lines within near_window of any changed line.
    near_lines: set[int] = set()
    for line_no in changed_lines:
        for offset in range(-near_window, near_window + 1):
            near_lines.add(line_no + offset)
    # Remove the changed lines themselves from near — those are "direct".
    near_lines -= changed_lines

    for line_no, author in blame.items():
        if line_no in changed_lines:
            direct_authors.add(author)
        elif line_no in near_lines:
            near_authors.add(author)

    # near_only: people near the changes but who didn't author the changed lines.
    near_only = near_authors - direct_authors
    return direct_authors, near_only


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
    head_ref: str | None = None,
) -> list[dict[str, object]]:
    """Fetch blame data for changed files, returning scorer-compatible format.

    For each file, runs ``git blame`` on the base ref and ``git diff`` between
    base and head to determine which lines actually changed. Authors are
    classified as:
    - ``authors``: authored lines that are being modified (full credit)
    - ``near_authors``: authored lines within NEAR_LINES_WINDOW of changes
      but not the changes themselves (half credit)

    Args:
        repo_path: Path to local repo clone.
        changed_files: List of file paths changed in the PR.
        base_ref: Base commit SHA or ref to blame against (e.g. the PR's
            base branch). This is what we run ``git blame`` on.
        head_ref: Head commit SHA or ref (e.g. the PR's head commit).
            Used for ``git diff base..head``. If None, diffs against the
            working tree (unreliable unless repo is checked out to PR head).

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

        # Get which lines actually changed so we can classify authors properly.
        changed_lines = _get_changed_lines_for_file(repo_path, file_path, base_ref, head_ref)

        if changed_lines:
            direct, near_only = _classify_authors(blame, changed_lines)
            results.append(
                {
                    "file_path": file_path,
                    "authors": sorted(direct),
                    "near_authors": sorted(near_only),
                }
            )
        else:
            # New file or diff unavailable — can't determine changed lines.
            # Skip rather than give false credit.
            logger.debug("No diff hunks for %s; skipping blame classification", file_path)

    return results
