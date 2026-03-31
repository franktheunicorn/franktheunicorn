"""Local repository manager for blame and analysis (v1.25).

Ensures local repo clones exist, are fetched, and have the necessary
refs available for blame and diff operations. Uses bare-style operations
where possible to avoid needing to checkout specific branches.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_repo(
    repos_dir: Path,
    owner: str,
    repo: str,
    *,
    clone_url: str = "",
) -> Path | None:
    """Ensure a local clone exists for the given repo.

    If the repo doesn't exist, clones it (checked out to the default branch).
    If it does, fetches latest and updates the working tree to the default
    branch tip so features that read files (copypasta, CodeRabbit) work.

    Returns the repo path, or None if cloning/fetching fails.
    """
    repo_path = repos_dir / owner / repo

    if repo_path.is_dir() and (repo_path / ".git").is_dir():
        # Already cloned — fetch latest and fast-forward the default branch.
        _git_fetch(repo_path)
        _update_default_branch(repo_path)
        return repo_path

    # Need to clone.
    if not clone_url:
        clone_url = f"https://github.com/{owner}/{repo}.git"

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", clone_url, str(repo_path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        logger.info("Cloned %s/%s to %s", owner, repo, repo_path)
        return repo_path
    except subprocess.CalledProcessError as exc:
        logger.warning("git clone failed for %s/%s: %s", owner, repo, exc.stderr[:200])
        return None
    except subprocess.TimeoutExpired:
        logger.warning("git clone timed out for %s/%s", owner, repo)
        return None


def _update_default_branch(repo_path: Path) -> None:
    """Fast-forward the working tree to the default branch tip.

    Keeps the local clone's working tree current with origin/main (or
    origin/master). This is needed for features that read files from the
    working tree (copypasta detection, CodeRabbit).
    """
    for branch in ("main", "master"):
        try:
            # Check if origin/<branch> exists.
            result = subprocess.run(
                ["git", "rev-parse", "--verify", f"origin/{branch}"],
                capture_output=True,
                text=True,
                cwd=str(repo_path),
                timeout=5,
            )
            if result.returncode != 0:
                continue

            # Reset working tree to this branch. Using reset --hard is safe
            # because the worker never makes local modifications.
            subprocess.run(
                ["git", "checkout", "-f", branch],
                capture_output=True,
                text=True,
                cwd=str(repo_path),
                timeout=15,
            )
            subprocess.run(
                ["git", "reset", "--hard", f"origin/{branch}"],
                capture_output=True,
                text=True,
                cwd=str(repo_path),
                timeout=15,
            )
            logger.debug("Updated %s to origin/%s", repo_path, branch)
            return
        except Exception:
            continue
    logger.debug("Could not determine default branch for %s", repo_path)


def _git_fetch(repo_path: Path) -> bool:
    """Fetch all refs from origin. Returns True on success."""
    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(repo_path),
            timeout=120,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def ensure_ref_available(repo_path: Path, sha: str) -> bool:
    """Check whether a given commit SHA is reachable in the local repo."""
    try:
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "commit"
    except Exception:
        return False


def fetch_ref(repo_path: Path, ref: str) -> bool:
    """Fetch a specific ref from origin (e.g. a PR head SHA or branch)."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", ref],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(repo_path),
            timeout=60,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
