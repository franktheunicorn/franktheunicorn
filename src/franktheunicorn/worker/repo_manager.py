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

    If the repo doesn't exist, clones it. If it does, fetches latest.
    Returns the repo path, or None if cloning/fetching fails.
    """
    repo_path = repos_dir / owner / repo

    if repo_path.is_dir() and (repo_path / ".git").is_dir():
        # Already cloned — just fetch latest.
        if _git_fetch(repo_path):
            return repo_path
        logger.warning("git fetch failed for %s/%s; using stale clone", owner, repo)
        return repo_path

    # Need to clone.
    if not clone_url:
        clone_url = f"https://github.com/{owner}/{repo}.git"

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--no-checkout", clone_url, str(repo_path)],
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
