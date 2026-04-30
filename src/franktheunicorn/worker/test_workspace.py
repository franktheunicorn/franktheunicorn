"""Git worktree helpers for the differential test runner (§9.2).

The runner needs two ephemeral checkouts:

* the PR head — tests run against the contributor's code
* the base branch with PR test files cherry-picked on top — tests run against
  base code with the new tests applied

Both are created via ``git worktree add`` so the underlying clone (managed by
``worker/repo_manager.py``) is never mutated. Each worktree is removed in a
context manager regardless of test outcome.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from franktheunicorn.worker.repo_manager import ensure_ref_available, fetch_ref

logger = logging.getLogger(__name__)


def _git(repo_path: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo_path),
        timeout=timeout,
    )


def _ensure_ref(repo_path: Path, ref: str) -> bool:
    """Make sure ``ref`` (a SHA or branch) is reachable locally; fetch if not."""
    if ensure_ref_available(repo_path, ref):
        return True
    if fetch_ref(repo_path, ref):
        return ensure_ref_available(repo_path, ref)
    return False


@contextmanager
def pr_branch_workspace(repo_path: Path, head_sha: str) -> Iterator[Path]:
    """Yield a worktree checked out at the PR head SHA. Cleans up on exit."""
    if not _ensure_ref(repo_path, head_sha):
        msg = f"PR head SHA {head_sha[:12]} not available in {repo_path}"
        raise RuntimeError(msg)

    work_root = Path(tempfile.mkdtemp(prefix="frank-test-pr-"))
    work_dir = work_root / "wt"
    proc = _git(repo_path, "worktree", "add", "--detach", str(work_dir), head_sha)
    if proc.returncode != 0:
        shutil.rmtree(work_root, ignore_errors=True)
        msg = f"git worktree add failed for PR head {head_sha[:12]}: {proc.stderr[:200]}"
        raise RuntimeError(msg)

    try:
        yield work_dir
    finally:
        _git(repo_path, "worktree", "remove", "--force", str(work_dir))
        shutil.rmtree(work_root, ignore_errors=True)


@contextmanager
def base_cherry_pick_workspace(
    repo_path: Path,
    base_sha: str,
    head_sha: str,
    test_files: list[str],
) -> Iterator[Path]:
    """Yield a worktree at ``base_sha`` with ``test_files`` taken from ``head_sha``.

    This implements the §9.2 "base + cherry-picked tests" path without doing a
    real ``git cherry-pick`` (which would attempt to replay the full PR commit
    range and routinely fail on production diffs). Instead we check out the
    base SHA, then ``git checkout <head_sha> -- <test_files>`` to overlay only
    the new/modified test files.
    """
    if not _ensure_ref(repo_path, base_sha):
        msg = f"Base SHA {base_sha[:12]} not available in {repo_path}"
        raise RuntimeError(msg)
    if test_files and not _ensure_ref(repo_path, head_sha):
        msg = f"PR head SHA {head_sha[:12]} not available in {repo_path}"
        raise RuntimeError(msg)

    work_root = Path(tempfile.mkdtemp(prefix="frank-test-base-"))
    work_dir = work_root / "wt"
    proc = _git(repo_path, "worktree", "add", "--detach", str(work_dir), base_sha)
    if proc.returncode != 0:
        shutil.rmtree(work_root, ignore_errors=True)
        msg = f"git worktree add failed for base {base_sha[:12]}: {proc.stderr[:200]}"
        raise RuntimeError(msg)

    try:
        if test_files:
            # Pull the test files (and only those) from the PR head onto base.
            # Missing files at head are skipped — they may be deletions in the
            # PR, in which case base already lacks the corresponding test.
            checkout = _git(work_dir, "checkout", head_sha, "--", *test_files)
            if checkout.returncode != 0:
                logger.debug(
                    "checkout of test files from %s onto base failed: %s",
                    head_sha[:12],
                    checkout.stderr[:200],
                )
        yield work_dir
    finally:
        _git(repo_path, "worktree", "remove", "--force", str(work_dir))
        shutil.rmtree(work_root, ignore_errors=True)
