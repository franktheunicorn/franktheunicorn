"""Tests for repo health analysis (git-based project context bootstrapping)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from franktheunicorn.worker.repo_health import (
    BugHotspot,
    ChurnEntry,
    ContributorEntry,
    MonthlyCommits,
    RepoHealthSnapshot,
    analyze_bug_hotspots,
    analyze_contributors,
    analyze_emergency_patterns,
    analyze_high_churn,
    analyze_momentum,
    analyze_repo_health,
    format_health_for_review,
    snapshot_from_dict,
    snapshot_to_dict,
)


def _git(repo: Path, *args: str) -> str:
    """Run a git command in the given repo with signing disabled."""
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a git repo with varied commit history for testing."""
    repo = tmp_path / "sample"
    repo.mkdir()
    subprocess.run(
        ["git", "init", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(repo, "config", "user.email", "alice@example.com")
    _git(repo, "config", "user.name", "Alice")

    # Commit 1: initial files
    (repo / "main.py").write_text("print('hello')\n")
    (repo / "utils.py").write_text("def helper(): pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")

    # Commit 2: bug fix touching main.py
    (repo / "main.py").write_text("print('hello world')\n")
    _git(repo, "add", "main.py")
    _git(repo, "commit", "-m", "fix: broken output in main")

    # Commit 3: another change to main.py (churn)
    (repo / "main.py").write_text("print('hello world!')\n")
    _git(repo, "add", "main.py")
    _git(repo, "commit", "-m", "Update greeting")

    # Commit 4: different author
    _git(repo, "config", "user.email", "bob@example.com")
    _git(repo, "config", "user.name", "Bob")
    (repo / "utils.py").write_text("def helper(): return True\n")
    _git(repo, "add", "utils.py")
    _git(repo, "commit", "-m", "Bug fix in utils")

    # Commit 5: emergency/revert
    _git(repo, "config", "user.email", "alice@example.com")
    _git(repo, "config", "user.name", "Alice")
    (repo / "main.py").write_text("print('hello world')\n")
    _git(repo, "add", "main.py")
    _git(repo, "commit", "-m", "Revert greeting change")

    return repo


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    """Create an empty git repo (no commits)."""
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(
        ["git", "init", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    return repo


class TestAnalyzeHighChurn:
    def test_returns_files_sorted_by_count(self, sample_repo: Path) -> None:
        result = analyze_high_churn(sample_repo, since="5 years ago")
        assert len(result) > 0
        # main.py was modified most (initial + fix + update + revert = 4 times)
        assert result[0].file_path == "main.py"
        assert result[0].commit_count >= 3

    def test_respects_limit(self, sample_repo: Path) -> None:
        result = analyze_high_churn(sample_repo, since="5 years ago", limit=1)
        assert len(result) == 1

    def test_empty_repo_returns_empty(self, empty_repo: Path) -> None:
        result = analyze_high_churn(empty_repo, since="5 years ago")
        assert result == []

    def test_nonexistent_path_returns_empty(self, tmp_path: Path) -> None:
        result = analyze_high_churn(tmp_path / "nonexistent")
        assert result == []


class TestAnalyzeContributors:
    def test_returns_contributors_sorted_by_count(self, sample_repo: Path) -> None:
        result = analyze_contributors(sample_repo)
        assert len(result) == 2
        # Alice has 4 commits, Bob has 1
        assert result[0].author == "Alice"
        assert result[0].commit_count == 4
        assert result[1].author == "Bob"
        assert result[1].commit_count == 1

    def test_empty_repo_returns_empty(self, empty_repo: Path) -> None:
        result = analyze_contributors(empty_repo)
        assert result == []

    def test_respects_limit(self, sample_repo: Path) -> None:
        result = analyze_contributors(sample_repo, limit=1)
        assert len(result) == 1


class TestAnalyzeBugHotspots:
    def test_finds_files_in_bug_commits(self, sample_repo: Path) -> None:
        result = analyze_bug_hotspots(sample_repo, since="5 years ago")
        assert len(result) > 0
        paths = {e.file_path for e in result}
        # "fix: broken output in main" and "Bug fix in utils" match
        assert "main.py" in paths
        assert "utils.py" in paths

    def test_empty_repo_returns_empty(self, empty_repo: Path) -> None:
        result = analyze_bug_hotspots(empty_repo, since="5 years ago")
        assert result == []


class TestAnalyzeMomentum:
    def test_returns_monthly_counts(self, sample_repo: Path) -> None:
        result = analyze_momentum(sample_repo)
        assert len(result) >= 1
        # All 5 commits are in the same month (now)
        total = sum(m.count for m in result)
        assert total == 5

    def test_months_are_sorted(self, sample_repo: Path) -> None:
        result = analyze_momentum(sample_repo)
        months = [m.month for m in result]
        assert months == sorted(months)

    def test_empty_repo_returns_empty(self, empty_repo: Path) -> None:
        result = analyze_momentum(empty_repo)
        assert result == []


class TestAnalyzeEmergencyPatterns:
    def test_finds_revert_commits(self, sample_repo: Path) -> None:
        result = analyze_emergency_patterns(sample_repo, since="5 years ago")
        assert len(result) >= 1
        assert any("Revert" in line for line in result)

    def test_empty_repo_returns_empty(self, empty_repo: Path) -> None:
        result = analyze_emergency_patterns(empty_repo, since="5 years ago")
        assert result == []


class TestAnalyzeRepoHealth:
    def test_runs_all_analyses(self, sample_repo: Path) -> None:
        snapshot = analyze_repo_health(sample_repo)
        assert snapshot.analyzed_at != ""
        assert len(snapshot.high_churn_files) > 0
        assert len(snapshot.contributors) > 0
        assert len(snapshot.bug_hotspots) > 0
        assert len(snapshot.monthly_commits) > 0
        assert len(snapshot.emergency_commits) > 0

    def test_returns_empty_snapshot_for_empty_repo(self, empty_repo: Path) -> None:
        snapshot = analyze_repo_health(empty_repo)
        assert snapshot.analyzed_at != ""
        assert snapshot.high_churn_files == []
        assert snapshot.contributors == []


class TestSnapshotSerialization:
    def test_roundtrip(self) -> None:
        original = RepoHealthSnapshot(
            high_churn_files=[ChurnEntry("a.py", 10)],
            contributors=[ContributorEntry("Alice", 50)],
            bug_hotspots=[BugHotspot("b.py", 5)],
            monthly_commits=[MonthlyCommits("2025-01", 30)],
            emergency_commits=["abc1234 Revert bad change"],
            analyzed_at="2025-06-01T00:00:00+00:00",
        )
        data = snapshot_to_dict(original)
        restored = snapshot_from_dict(data)

        assert len(restored.high_churn_files) == 1
        assert restored.high_churn_files[0].file_path == "a.py"
        assert restored.high_churn_files[0].commit_count == 10
        assert len(restored.contributors) == 1
        assert restored.contributors[0].author == "Alice"
        assert len(restored.bug_hotspots) == 1
        assert restored.bug_hotspots[0].bug_commit_count == 5
        assert len(restored.monthly_commits) == 1
        assert restored.monthly_commits[0].month == "2025-01"
        assert len(restored.emergency_commits) == 1
        assert restored.analyzed_at == "2025-06-01T00:00:00+00:00"

    def test_from_empty_dict(self) -> None:
        snapshot = snapshot_from_dict({})
        assert snapshot.high_churn_files == []
        assert snapshot.analyzed_at == ""

    def test_from_none_like(self) -> None:
        snapshot = snapshot_from_dict({})
        assert snapshot.emergency_commits == []


class TestFormatHealthForReview:
    def test_highlights_changed_files_in_churn_list(self) -> None:
        snapshot = RepoHealthSnapshot(
            high_churn_files=[
                ChurnEntry("src/main.py", 47),
                ChurnEntry("src/utils.py", 12),
            ],
            bug_hotspots=[BugHotspot("src/main.py", 15)],
            contributors=[
                ContributorEntry("Alice", 60),
                ContributorEntry("Bob", 40),
            ],
            monthly_commits=[
                MonthlyCommits("2025-04", 20),
                MonthlyCommits("2025-05", 25),
                MonthlyCommits("2025-06", 30),
            ],
            emergency_commits=["abc Revert", "def hotfix"],
            analyzed_at="2025-06-01T00:00:00+00:00",
        )

        result = format_health_for_review(snapshot, ["src/main.py", "README.md"])
        assert "src/main.py" in result
        assert "high-churn" in result
        assert "bug hotspot" in result
        # README.md not in churn/hotspot lists, so not flagged
        assert "README.md" not in result

    def test_includes_project_summary(self) -> None:
        snapshot = RepoHealthSnapshot(
            high_churn_files=[],
            bug_hotspots=[],
            contributors=[
                ContributorEntry("Alice", 90),
                ContributorEntry("Bob", 10),
            ],
            monthly_commits=[MonthlyCommits("2025-06", 30)],
            emergency_commits=["abc Revert"],
            analyzed_at="2025-06-01T00:00:00+00:00",
        )

        result = format_health_for_review(snapshot, ["any_file.py"])
        assert "2 contributors" in result
        assert "90%" in result
        assert "1 emergency" in result
        assert "30 commits/month" in result

    def test_returns_empty_for_no_snapshot(self) -> None:
        snapshot = RepoHealthSnapshot()
        result = format_health_for_review(snapshot, ["file.py"])
        assert result == ""

    def test_no_file_notes_when_no_overlap(self) -> None:
        snapshot = RepoHealthSnapshot(
            high_churn_files=[ChurnEntry("other.py", 10)],
            bug_hotspots=[],
            contributors=[ContributorEntry("Alice", 100)],
            monthly_commits=[],
            emergency_commits=[],
            analyzed_at="2025-06-01T00:00:00+00:00",
        )
        result = format_health_for_review(snapshot, ["unrelated.py"])
        assert "Files in this PR" not in result
