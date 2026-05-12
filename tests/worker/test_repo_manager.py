"""Tests for the local repo manager (v1.25)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from franktheunicorn.worker.repo_manager import (
    _ensure_fork_remote,
    _git_fetch,
    _git_fetch_with_backoff,
    ensure_ref_available,
    ensure_repo,
    ensure_sha_fetched,
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


class TestGitFetchWithBackoff:
    def test_succeeds_on_first_attempt(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None
        assert _git_fetch_with_backoff(repo, "org", "repo") is True

    def test_returns_false_after_exhausting_retries(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        with (
            patch("franktheunicorn.worker.repo_manager._git_fetch", return_value=False),
            patch("franktheunicorn.worker.repo_manager.time.sleep"),
        ):
            assert _git_fetch_with_backoff(tmp_path, "org", "repo") is False

    def test_retries_correct_number_of_times(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        with (
            patch(
                "franktheunicorn.worker.repo_manager._git_fetch", return_value=False
            ) as mock_fetch,
            patch("franktheunicorn.worker.repo_manager.time.sleep"),
        ):
            _git_fetch_with_backoff(tmp_path, "org", "repo")
        # 4 delays in _FETCH_BACKOFF_DELAYS → 5 total attempts
        assert mock_fetch.call_count == 5

    def test_succeeds_on_second_attempt(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        with (
            patch(
                "franktheunicorn.worker.repo_manager._git_fetch", side_effect=[False, True]
            ) as mock_fetch,
            patch("franktheunicorn.worker.repo_manager.time.sleep") as mock_sleep,
        ):
            result = _git_fetch_with_backoff(tmp_path, "org", "repo")
        assert result is True
        assert mock_fetch.call_count == 2
        mock_sleep.assert_called_once()

    def test_backoff_warning_fires_for_60s_delay(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from unittest.mock import patch

        with (
            patch("franktheunicorn.worker.repo_manager._git_fetch", return_value=False),
            patch("franktheunicorn.worker.repo_manager.time.sleep"),
            caplog.at_level("WARNING"),
        ):
            _git_fetch_with_backoff(tmp_path, "org", "repo")
        backoff_warnings = [r for r in caplog.records if "Backing off" in r.message]
        assert backoff_warnings, "Expected 'Backing off' warning when delay >= 60s"


def _add_commit(work: Path, filename: str, content: str, message: str) -> str:
    """Add a file and commit in *work*; return the new commit SHA."""
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "test")
    (work / filename).write_text(content)
    _git(work, "add", ".")
    _git(work, "commit", "-m", message)
    return _git(work, "rev-parse", "HEAD")


class TestEnsureShaFetched:
    """Tests for ensure_sha_fetched — the multi-strategy ref fetcher."""

    def test_already_available_returns_true(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None
        sha = _git(repo, "rev-parse", "HEAD")
        # SHA already present — should return True without any fetch.
        assert ensure_sha_fetched(repo, sha) is True

    def test_branch_fetch_succeeds(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None

        # Push a new commit on a feature branch to the bare remote.
        work = tmp_path / "work2"
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "clone", str(bare_repo), str(work)],
            capture_output=True,
            text=True,
            check=True,
        )
        _git(work, "checkout", "-b", "feature")
        new_sha = _add_commit(work, "feature.txt", "feature\n", "feature commit")
        _git(work, "push", "origin", "feature")

        assert ensure_ref_available(repo, new_sha) is False
        assert ensure_sha_fetched(repo, new_sha, branch="feature") is True

    def test_pr_ref_fetch_succeeds(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None

        # Push a commit to refs/pull/5/head on the bare remote.
        work = tmp_path / "work2"
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "clone", str(bare_repo), str(work)],
            capture_output=True,
            text=True,
            check=True,
        )
        new_sha = _add_commit(work, "pr_file.txt", "pr content\n", "pr commit")
        subprocess.run(
            ["git", "push", str(bare_repo), "HEAD:refs/pull/5/head"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(work),
        )

        assert ensure_ref_available(repo, new_sha) is False
        assert ensure_sha_fetched(repo, new_sha, pr_number=5) is True

    def test_fork_remote_added_and_fetched(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None

        # Create a fork bare repo with a commit not present in the main clone.
        fork_remote = tmp_path / "fork.git"
        subprocess.run(
            ["git", "init", "--bare", str(fork_remote)],
            capture_output=True,
            text=True,
            check=True,
        )
        fork_work = tmp_path / "fork_work"
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "clone", str(bare_repo), str(fork_work)],
            capture_output=True,
            text=True,
            check=True,
        )
        fork_sha = _add_commit(fork_work, "fork_file.txt", "fork content\n", "fork commit")
        subprocess.run(
            ["git", "push", str(fork_remote), "HEAD:main"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(fork_work),
        )

        assert ensure_ref_available(repo, fork_sha) is False
        assert ensure_sha_fetched(repo, fork_sha, fork_clone_url=str(fork_remote)) is True

    def test_fork_remote_already_exists_url_updated(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None

        fork_remote = tmp_path / "fork.git"
        subprocess.run(
            ["git", "init", "--bare", str(fork_remote)],
            capture_output=True,
            text=True,
            check=True,
        )
        fork_clone_url = str(fork_remote)

        # First call: add the remote.
        remote_name = _ensure_fork_remote(repo, fork_clone_url)

        # Corrupt the URL so we can verify it gets updated.
        subprocess.run(
            ["git", "remote", "set-url", remote_name, "/old/path.git"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(repo),
        )

        # Second call: should detect the changed URL and update it.
        _ensure_fork_remote(repo, fork_clone_url)

        result = subprocess.run(
            ["git", "remote", "get-url", remote_name],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(repo),
        )
        assert result.stdout.strip() == fork_clone_url

    def test_all_strategies_fail_returns_false(self, tmp_path: Path, bare_repo: Path) -> None:
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        repo = ensure_repo(repos_dir, "org", "repo", clone_url=str(bare_repo))
        assert repo is not None

        unknown_sha = "deadbeef" * 5  # 40 hex chars, doesn't exist anywhere
        assert ensure_sha_fetched(repo, unknown_sha) is False
