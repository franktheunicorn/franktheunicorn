"""Tests for the local repo manager (v1.25)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from franktheunicorn.worker.repo_manager import (
    _git_fetch,
    ensure_ref_available,
    ensure_repo,
    fetch_ref,
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
def bare_repo(tmp_path: Path) -> Path:
    """Create a bare git repo to serve as a 'remote'."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        capture_output=True,
        text=True,
        check=True,
    )

    # Create a temporary working clone to push an initial commit.
    work = tmp_path / "work"
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "clone", str(remote), str(work)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "test")
    (work / "README.md").write_text("# test\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "HEAD")
    return remote


class TestEnsureRepo:
    def test_clones_new_repo(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        result = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert result is not None
        assert (result / ".git").is_dir()

    def test_fetches_existing_repo(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        # First call clones
        first = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert first is not None
        # Second call fetches
        second = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert second is not None
        assert first == second

    def test_returns_none_for_bad_url(self, tmp_path: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        result = ensure_repo(repos_dir, "org", "repo", clone_url="/nonexistent/path.git")
        assert result is None


class TestEnsureRefAvailable:
    def test_valid_sha(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None
        sha = _git(repo, "rev-parse", "HEAD")
        assert ensure_ref_available(repo, sha) is True

    def test_invalid_sha(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None
        assert ensure_ref_available(repo, "0" * 40) is False

    def test_not_a_repo(self, tmp_path: Path) -> None:
        assert ensure_ref_available(tmp_path, "a" * 40) is False


class TestGitFetch:
    def test_fetch_failure(self, tmp_path: Path) -> None:
        """Fetch on a non-repo dir returns False."""
        assert _git_fetch(tmp_path) is False

    def test_fetch_success(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None
        assert _git_fetch(repo) is True


class TestFetchRef:
    def test_fetch_ref_success(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None
        # Fetch HEAD (known ref) should succeed
        sha = _git(repo, "rev-parse", "HEAD")
        assert fetch_ref(repo, sha) is True

    def test_fetch_ref_failure(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None
        # Fetching a nonexistent ref should fail
        assert fetch_ref(repo, "nonexistent-branch-xyz") is False

    def test_fetch_ref_not_a_repo(self, tmp_path: Path) -> None:
        assert fetch_ref(tmp_path, "main") is False


class TestEnsureRepoTimeout:
    def test_clone_timeout(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        with patch(
            "franktheunicorn.worker.repo_manager.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git clone", timeout=300),
        ):
            result = ensure_repo(repos_dir, "org", "slow-repo")
        assert result is None
