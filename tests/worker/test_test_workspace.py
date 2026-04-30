"""Tests for git worktree helpers used by the differential test runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from franktheunicorn.worker.test_workspace import (
    base_cherry_pick_workspace,
    pr_branch_workspace,
)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(repo),
    )
    return out.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Bootstrap a tiny repo with two commits.

    Returns ``(repo, base_sha, head_sha)`` where ``base_sha`` has only
    ``src/main.py`` and ``head_sha`` adds ``tests/test_main.py`` plus modifies
    ``src/main.py``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "frank@example.com")
    _git(repo, "config", "user.name", "Frank")
    # Disable signing locally — CI / dev environments may have a global
    # gpgsign setting that the test repo can't satisfy.
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgsign", "false")

    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", "src/main.py")
    _git(repo, "commit", "-m", "initial")
    base_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "src" / "main.py").write_text("def add(a, b):\n    return a + b + 0\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_main.py").write_text(
        "from src.main import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add tests + tweak")
    head_sha = _git(repo, "rev-parse", "HEAD")
    return repo, base_sha, head_sha


def test_pr_branch_workspace_yields_head_state(
    git_repo: tuple[Path, str, str],
) -> None:
    repo, _base, head = git_repo
    with pr_branch_workspace(repo, head) as ws:
        assert (ws / "tests" / "test_main.py").is_file()
        # Head modification visible.
        assert "+ 0" in (ws / "src" / "main.py").read_text()
    # Worktree cleaned up.
    assert not ws.exists()


def test_base_cherry_pick_overlays_test_files(
    git_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = git_repo
    with base_cherry_pick_workspace(repo, base, head, ["tests/test_main.py"]) as ws:
        # Test file from head was overlaid.
        assert (ws / "tests" / "test_main.py").is_file()
        # Source code is still base (no "+ 0" tweak).
        assert "+ 0" not in (ws / "src" / "main.py").read_text()
    assert not ws.exists()


def test_base_cherry_pick_with_no_test_files(
    git_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = git_repo
    with base_cherry_pick_workspace(repo, base, head, []) as ws:
        # Pure base checkout — no overlay performed.
        assert not (ws / "tests" / "test_main.py").exists()
        assert "+ 0" not in (ws / "src" / "main.py").read_text()
    assert not ws.exists()


def test_pr_branch_workspace_unknown_sha_raises(
    git_repo: tuple[Path, str, str],
) -> None:
    repo, _base, _head = git_repo
    bogus = "0" * 40
    with pytest.raises(RuntimeError), pr_branch_workspace(repo, bogus):
        pass
