"""Repository health analysis for bootstrapping project context.

Runs git commands on local clones to extract codebase health signals:
high-churn files, contributor distribution, bug hotspots, project
momentum, and emergency patterns. Inspired by
https://piechowski.io/post/git-commands-before-reading-code/

Pure functions — no Django imports. Results feed into the LLM review
pipeline and are stored as JSON on the Project model.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT = 120


@dataclass
class ChurnEntry:
    """A file and how many commits touched it."""

    file_path: str
    commit_count: int


@dataclass
class ContributorEntry:
    """A contributor and their commit count."""

    author: str
    commit_count: int


@dataclass
class BugHotspot:
    """A file and how many bug-related commits touched it."""

    file_path: str
    bug_commit_count: int


@dataclass
class MonthlyCommits:
    """Commit count for a single calendar month."""

    month: str  # "2025-03"
    count: int


@dataclass
class RepoHealthSnapshot:
    """Aggregated health analysis of a repository."""

    high_churn_files: list[ChurnEntry] = field(default_factory=list)
    contributors: list[ContributorEntry] = field(default_factory=list)
    bug_hotspots: list[BugHotspot] = field(default_factory=list)
    monthly_commits: list[MonthlyCommits] = field(default_factory=list)
    emergency_commits: list[str] = field(default_factory=list)
    analyzed_at: str = ""


def _run_git(repo_path: Path, args: list[str]) -> str | None:
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=_SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.debug("git %s failed: %s", args[0], result.stderr[:200])
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git %s error: %s", args[0], exc)
        return None


# ---------------------------------------------------------------------------
# Individual analyses
# ---------------------------------------------------------------------------


def analyze_high_churn(
    repo_path: Path,
    *,
    since: str = "12 months ago",
    limit: int = 20,
) -> list[ChurnEntry]:
    """Find the most frequently modified files over a time period."""
    output = _run_git(repo_path, ["log", "--format=format:", "--name-only", f"--since={since}"])
    if output is None:
        return []

    counts: Counter[str] = Counter()
    for line in output.splitlines():
        name = line.strip()
        if name:
            counts[name] += 1

    return [
        ChurnEntry(file_path=path, commit_count=count) for path, count in counts.most_common(limit)
    ]


def analyze_contributors(
    repo_path: Path,
    *,
    limit: int = 50,
) -> list[ContributorEntry]:
    """Rank contributors by commit count (excludes merges)."""
    output = _run_git(repo_path, ["shortlog", "-sn", "--no-merges", "HEAD"])
    if output is None:
        return []

    entries: list[ContributorEntry] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "  123\tAuthor Name"
        parts = line.split("\t", 1)
        if len(parts) == 2:
            try:
                count = int(parts[0].strip())
                author = parts[1].strip()
                if author:
                    entries.append(ContributorEntry(author=author, commit_count=count))
            except ValueError:
                continue

    return entries[:limit]


def analyze_bug_hotspots(
    repo_path: Path,
    *,
    since: str = "12 months ago",
    limit: int = 20,
) -> list[BugHotspot]:
    """Find files most frequently touched in bug-fix commits."""
    output = _run_git(
        repo_path,
        [
            "log",
            "-i",
            "-E",
            "--grep=fix|bug|broken",
            "--name-only",
            "--format=",
            f"--since={since}",
        ],
    )
    if output is None:
        return []

    counts: Counter[str] = Counter()
    for line in output.splitlines():
        name = line.strip()
        if name:
            counts[name] += 1

    return [
        BugHotspot(file_path=path, bug_commit_count=count)
        for path, count in counts.most_common(limit)
    ]


def analyze_momentum(repo_path: Path) -> list[MonthlyCommits]:
    """Show commit frequency by month across the entire repo history."""
    output = _run_git(repo_path, ["log", "--format=%ad", "--date=format:%Y-%m"])
    if output is None:
        return []

    counts: Counter[str] = Counter()
    for line in output.splitlines():
        month = line.strip()
        if month:
            counts[month] += 1

    return [MonthlyCommits(month=month, count=count) for month, count in sorted(counts.items())]


_EMERGENCY_RE = re.compile(r"revert|hotfix|emergency|rollback", re.IGNORECASE)


def analyze_emergency_patterns(
    repo_path: Path,
    *,
    since: str = "12 months ago",
) -> list[str]:
    """Find commits with emergency-related keywords (reverts, hotfixes, etc.)."""
    output = _run_git(repo_path, ["log", "--oneline", f"--since={since}"])
    if output is None:
        return []

    return [
        line.strip() for line in output.splitlines() if line.strip() and _EMERGENCY_RE.search(line)
    ]


# ---------------------------------------------------------------------------
# Full snapshot
# ---------------------------------------------------------------------------


def analyze_repo_health(repo_path: Path) -> RepoHealthSnapshot:
    """Run all five analyses and return a combined snapshot.

    Individual analyses that fail are logged and returned as empty lists;
    the overall snapshot is never None.
    """
    now = datetime.now(UTC).isoformat()
    snapshot = RepoHealthSnapshot(analyzed_at=now)

    snapshot.high_churn_files = analyze_high_churn(repo_path)
    snapshot.contributors = analyze_contributors(repo_path)
    snapshot.bug_hotspots = analyze_bug_hotspots(repo_path)
    snapshot.monthly_commits = analyze_momentum(repo_path)
    snapshot.emergency_commits = analyze_emergency_patterns(repo_path)

    return snapshot


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def snapshot_to_dict(snapshot: RepoHealthSnapshot) -> dict[str, object]:
    """Convert a snapshot to a JSON-serializable dict."""
    return asdict(snapshot)


def snapshot_from_dict(data: dict[str, object]) -> RepoHealthSnapshot:
    """Reconstruct a snapshot from a dict (e.g. from JSONField)."""
    if not data:
        return RepoHealthSnapshot()

    def _raw_list(key: str) -> list[dict[str, object]]:
        raw = data.get(key, [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _int(d: dict[str, object], key: str) -> int:
        val = d.get(key, 0)
        return int(val) if isinstance(val, (int, float, str)) else 0

    emergency_raw = data.get("emergency_commits")
    emergency: list[str] = []
    if isinstance(emergency_raw, list):
        emergency = [s for s in emergency_raw if isinstance(s, str)]

    return RepoHealthSnapshot(
        high_churn_files=[
            ChurnEntry(file_path=str(d.get("file_path", "")), commit_count=_int(d, "commit_count"))
            for d in _raw_list("high_churn_files")
        ],
        contributors=[
            ContributorEntry(author=str(d.get("author", "")), commit_count=_int(d, "commit_count"))
            for d in _raw_list("contributors")
        ],
        bug_hotspots=[
            BugHotspot(
                file_path=str(d.get("file_path", "")),
                bug_commit_count=_int(d, "bug_commit_count"),
            )
            for d in _raw_list("bug_hotspots")
        ],
        monthly_commits=[
            MonthlyCommits(month=str(d.get("month", "")), count=_int(d, "count"))
            for d in _raw_list("monthly_commits")
        ],
        emergency_commits=emergency,
        analyzed_at=str(data.get("analyzed_at", "")),
    )


# ---------------------------------------------------------------------------
# Review context formatting
# ---------------------------------------------------------------------------


def format_health_for_review(
    snapshot: RepoHealthSnapshot,
    changed_files: list[str],
) -> str:
    """Format repo health insights relevant to the PR's changed files.

    Only surfaces per-file signals (churn, bug hotspot) for files that
    overlap with the PR diff. Always includes a brief project-level summary
    (top contributors, momentum trend, emergency count).
    """
    if not snapshot.analyzed_at:
        return ""

    parts: list[str] = []

    # Per-file signals: flag changed files that appear in churn/hotspot lists
    churn_lookup = {e.file_path: e.commit_count for e in snapshot.high_churn_files}
    hotspot_lookup = {e.file_path: e.bug_commit_count for e in snapshot.bug_hotspots}

    file_notes: list[str] = []
    for f in changed_files:
        notes: list[str] = []
        if f in churn_lookup:
            notes.append(f"high-churn ({churn_lookup[f]} commits/year)")
        if f in hotspot_lookup:
            notes.append(f"bug hotspot ({hotspot_lookup[f]} bug-fix commits/year)")
        if notes:
            file_notes.append(f"  {f}: {', '.join(notes)}")

    if file_notes:
        parts.append("Files in this PR with notable history:")
        parts.extend(file_notes)

    # Project-level summary
    summary: list[str] = []

    if snapshot.contributors:
        top = snapshot.contributors[0]
        total = sum(c.commit_count for c in snapshot.contributors)
        pct = round(top.commit_count / total * 100) if total else 0
        n = len(snapshot.contributors)
        summary.append(f"{n} contributors; top contributor ({top.author}) has {pct}% of commits")

    if snapshot.emergency_commits:
        summary.append(f"{len(snapshot.emergency_commits)} emergency commits in past year")

    if snapshot.monthly_commits:
        recent = snapshot.monthly_commits[-3:]
        avg = sum(m.count for m in recent) // len(recent) if recent else 0
        summary.append(f"~{avg} commits/month (recent 3-month avg)")

    if summary:
        parts.append("Project signals: " + "; ".join(summary) + ".")

    return "\n".join(parts)
