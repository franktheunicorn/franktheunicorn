"""Tests for the CVE file history fetcher."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from franktheunicorn.scoring.cve_history import (
    CACHE_TTL_SECONDS,
    _cache,
    _has_issue_link,
    _is_build_file,
    _is_skip_commit,
    _scan_cve_grep,
    _scan_terse_commits,
    fetch_cve_affected_files,
)


def _git(repo: Path, *args: str) -> None:
    """Run a git command in the given repo with signing disabled."""
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with various commit types.

    Commits:
    1. [SPARK-123] Add feature — normal commit with JIRA link
    2. Fix CVE-2024-12345 in auth module — CVE commit touching src/auth.py
    3. Fix null check — terse commit, no JIRA link, touches src/crypto.py
    4. Merge branch 'feature' — merge commit (should be skipped)
    5. [SPARK-456] Bump deps — normal commit touching pom.xml (build file)
    6. Fix CVE-2023-99999 dependency update — CVE commit touching only pom.xml
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "dev")

    # 1. Normal commit with JIRA link
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("# feature\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "[SPARK-123] Add feature")

    # 2. CVE commit
    (repo / "src" / "auth.py").write_text("# auth fix\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Fix CVE-2024-12345 in auth module")

    # 3. Terse commit (no JIRA link, short message)
    (repo / "src" / "crypto.py").write_text("# crypto fix\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Fix null check")

    # 4. Merge-style commit (should be skipped by terse heuristic)
    (repo / "src" / "merged.py").write_text("# merged\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Merge branch 'feature' into main")

    # 5. Normal commit touching build file
    (repo / "pom.xml").write_text("<project/>\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "[SPARK-456] Bump deps")

    # 6. CVE commit touching only build file (should be filtered out)
    (repo / "pom.xml").write_text("<project><version>2</version></project>\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Fix CVE-2023-99999 dependency update")

    return repo


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Clear the module-level cache before each test."""
    _cache.clear()


class TestIsBuildFile:
    def test_pom_xml(self) -> None:
        assert _is_build_file("pom.xml") is True

    def test_nested_pom_xml(self) -> None:
        assert _is_build_file("modules/core/pom.xml") is True

    def test_package_json(self) -> None:
        assert _is_build_file("package.json") is True

    def test_requirements_txt(self) -> None:
        assert _is_build_file("requirements.txt") is True

    def test_gradle_extension(self) -> None:
        assert _is_build_file("app/build.gradle") is True

    def test_lock_extension(self) -> None:
        assert _is_build_file("poetry.lock") is True

    def test_python_source(self) -> None:
        assert _is_build_file("src/main.py") is False

    def test_java_source(self) -> None:
        assert _is_build_file("src/Main.java") is False


class TestHasIssueLink:
    def test_jira_bracket(self) -> None:
        assert _has_issue_link("[SPARK-12345] Fix bug") is True

    def test_github_hash(self) -> None:
        assert _has_issue_link("Fix #123") is True

    def test_bare_jira(self) -> None:
        assert _has_issue_link("SPARK-123 Fix bug") is True

    def test_url_issue(self) -> None:
        assert _has_issue_link("Fix https://github.com/org/repo/issues/42") is True

    def test_url_pull(self) -> None:
        assert _has_issue_link("Related to https://github.com/org/repo/pull/99") is True

    def test_no_link(self) -> None:
        assert _has_issue_link("Fix null check") is False

    def test_empty(self) -> None:
        assert _has_issue_link("") is False


class TestIsSkipCommit:
    def test_merge(self) -> None:
        assert _is_skip_commit("Merge branch 'feature'") is True

    def test_merge_case_insensitive(self) -> None:
        assert _is_skip_commit("merge pull request #42") is True

    def test_revert(self) -> None:
        assert _is_skip_commit('Revert "Add feature"') is True

    def test_release(self) -> None:
        assert _is_skip_commit("Preparing for release 3.5.0") is True

    def test_version_bump(self) -> None:
        assert _is_skip_commit("version bump to 2.0") is True

    def test_normal_commit(self) -> None:
        assert _is_skip_commit("Fix null check in parser") is False


class TestScanCveGrep:
    def test_finds_cve_files(self, git_repo: Path) -> None:
        files = _scan_cve_grep(git_repo)
        assert "src/auth.py" in files

    def test_excludes_build_files(self, git_repo: Path) -> None:
        files = _scan_cve_grep(git_repo)
        assert "pom.xml" not in files

    def test_excludes_non_cve_commits(self, git_repo: Path) -> None:
        files = _scan_cve_grep(git_repo)
        # feature.py is from a normal commit, not a CVE commit
        assert "src/feature.py" not in files

    def test_empty_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "--initial-branch=main")
        _git(repo, "config", "user.email", "test@test.com")
        _git(repo, "config", "user.name", "test")
        (repo / "README.md").write_text("# empty\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "Initial")
        files = _scan_cve_grep(repo)
        assert files == set()


class TestScanTerseCommits:
    def test_finds_terse_files(self, git_repo: Path) -> None:
        files = _scan_terse_commits(git_repo)
        assert "src/crypto.py" in files

    def test_excludes_jira_linked(self, git_repo: Path) -> None:
        files = _scan_terse_commits(git_repo)
        assert "src/feature.py" not in files

    def test_excludes_merge_commits(self, git_repo: Path) -> None:
        files = _scan_terse_commits(git_repo)
        assert "src/merged.py" not in files

    def test_excludes_build_files(self, git_repo: Path) -> None:
        files = _scan_terse_commits(git_repo)
        assert "pom.xml" not in files

    def test_excludes_cve_commit(self, git_repo: Path) -> None:
        """CVE-YYYY-NNNNN in message matches bare JIRA pattern, so excluded."""
        files = _scan_terse_commits(git_repo)
        assert "src/auth.py" not in files


class TestFetchCveAffectedFiles:
    def test_standard_governance_uses_cve_grep(self, git_repo: Path) -> None:
        files = fetch_cve_affected_files(git_repo, governance="standard")
        assert "src/auth.py" in files
        # Terse commit files should NOT appear in standard governance
        assert "src/crypto.py" not in files

    def test_asf_governance_uses_terse_heuristic(self, git_repo: Path) -> None:
        files = fetch_cve_affected_files(git_repo, governance="asf")
        assert "src/crypto.py" in files
        # CVE commit has "CVE-2024-12345" which matches bare JIRA pattern,
        # so it's not considered terse. That's fine — ASF stealth fixes
        # wouldn't mention CVE identifiers anyway.
        assert "src/auth.py" not in files

    def test_extra_cve_files_merged(self, git_repo: Path) -> None:
        files = fetch_cve_affected_files(
            git_repo,
            governance="standard",
            extra_cve_files=["src/manual_cve.py"],
        )
        assert "src/manual_cve.py" in files
        assert "src/auth.py" in files

    def test_caching(self, git_repo: Path) -> None:
        result1 = fetch_cve_affected_files(git_repo, governance="standard")
        # Add a new CVE commit — should not appear due to cache
        (git_repo / "src" / "new_vuln.py").write_text("# vuln\n")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "Fix CVE-2025-11111 new vuln")
        result2 = fetch_cve_affected_files(git_repo, governance="standard")
        assert result1 == result2
        assert "src/new_vuln.py" not in result2

    def test_cache_expiry(self, git_repo: Path) -> None:
        fetch_cve_affected_files(git_repo, governance="standard")
        # Manually expire the cache
        key = f"{git_repo}:standard"
        cached_time, cached_files = _cache[key]
        _cache[key] = (cached_time - CACHE_TTL_SECONDS - 1, cached_files)
        # Add a new CVE commit
        (git_repo / "src" / "new_vuln.py").write_text("# vuln\n")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "Fix CVE-2025-22222 another vuln")
        result = fetch_cve_affected_files(git_repo, governance="standard")
        assert "src/new_vuln.py" in result

    def test_extra_cve_files_not_cached(self, git_repo: Path) -> None:
        """Extra files from config are merged fresh each call, not cached."""
        result1 = fetch_cve_affected_files(
            git_repo, governance="standard", extra_cve_files=["extra1.py"]
        )
        result2 = fetch_cve_affected_files(
            git_repo, governance="standard", extra_cve_files=["extra2.py"]
        )
        assert "extra1.py" in result1
        assert "extra2.py" in result2
        assert "extra1.py" not in result2

    def test_returns_sorted(self, git_repo: Path) -> None:
        files = fetch_cve_affected_files(
            git_repo,
            governance="standard",
            extra_cve_files=["zzz.py", "aaa.py"],
        )
        assert files == sorted(files)

    def test_governance_keyed_cache(self, git_repo: Path) -> None:
        """Different governance values should use separate cache entries."""
        standard = fetch_cve_affected_files(git_repo, governance="standard")
        asf = fetch_cve_affected_files(git_repo, governance="asf")
        # Standard finds CVE-mentioning commits, ASF finds terse commits
        assert standard != asf

    def test_nonexistent_repo(self, tmp_path: Path) -> None:
        result = fetch_cve_affected_files(tmp_path / "nonexistent")
        assert result == []
