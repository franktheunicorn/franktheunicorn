"""Local repository manager for blame and analysis (v1.25).

Ensures local repo clones exist, are fetched, and have the necessary
refs available for blame and diff operations. Uses bare-style operations
where possible to avoid needing to checkout specific branches.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import time
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
        if not _git_fetch_with_backoff(repo_path, owner, repo):
            logger.warning("git fetch failed for %s/%s; using stale clone", owner, repo)
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


_FETCH_BACKOFF_DELAYS = (5, 15, 60, 300)


def _git_fetch_with_backoff(repo_path: Path, owner: str, repo: str) -> bool:
    """Fetch all refs from origin with exponential backoff. Returns True on success."""
    cumulative_sleep = 0
    for attempt, _sentinel in enumerate((*_FETCH_BACKOFF_DELAYS, None)):
        if _git_fetch(repo_path):
            return True
        if _sentinel is None:
            break
        delay = _sentinel
        cumulative_sleep += delay
        if delay >= 60:
            logger.warning(
                "Backing off %ds after git fetch failure for %s/%s (attempt %d/%d)",
                delay,
                owner,
                repo,
                attempt + 1,
                len(_FETCH_BACKOFF_DELAYS),
            )
        else:
            logger.debug(
                "Retrying git fetch for %s/%s after %ds (attempt %d/%d) ...",
                owner,
                repo,
                delay,
                attempt + 1,
                len(_FETCH_BACKOFF_DELAYS),
            )
        time.sleep(delay)
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


def _ensure_fork_remote(repo_path: Path, clone_url: str) -> str:
    """Add or update a git remote for a fork URL.

    Derives a stable remote name from *clone_url*, adds the remote if it
    doesn't exist, or updates its URL if it has changed.  Returns the
    remote name.
    """
    # Derive a deterministic, filesystem-safe remote name from the URL.
    # Include an 8-hex hash of the full URL so that two different URLs that
    # share a long common prefix don't silently collide after truncation.
    url = clone_url
    url = re.sub(r"^git@[^:]+:", "", url)  # strip git@host:
    url = re.sub(r"^https?://[^/]+/", "", url)  # strip https://host/
    url = url.removesuffix(".git")
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "-", url).lstrip("-")
    url_hash = hashlib.sha256(clone_url.encode()).hexdigest()[:8]
    # "fork-" (5) + sanitized[:36] (≤36) + "-" (1) + hash (8) = ≤50 chars
    remote_name = f"fork-{sanitized[:36]}-{url_hash}"

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", remote_name],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=5,
        )
        if result.returncode == 0:
            existing_url = result.stdout.strip()
            if existing_url != clone_url:
                subprocess.run(
                    ["git", "remote", "set-url", remote_name, clone_url],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=str(repo_path),
                    timeout=5,
                )
                logger.debug("Updated fork remote %s URL to %s", remote_name, clone_url)
        else:
            subprocess.run(
                ["git", "remote", "add", remote_name, clone_url],
                capture_output=True,
                text=True,
                check=True,
                cwd=str(repo_path),
                timeout=5,
            )
            logger.debug("Added fork remote %s -> %s", remote_name, clone_url)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.debug("Could not add/update fork remote %s: %s", remote_name, exc)

    return remote_name


def ensure_sha_fetched(
    repo_path: Path,
    sha: str,
    *,
    branch: str = "",
    pr_number: int = 0,
    fork_clone_url: str = "",
) -> bool:
    """Ensure a commit SHA is available locally, fetching it if needed.

    Attempts to obtain the commit in this order:

    1. Already present — return immediately.
    2. Branch-based fetch from origin (if *branch* is given).
    3. PR ref fetch from origin (``refs/pull/{pr_number}/head``, if *pr_number* > 0).
    4. Fork remote fetch (if *fork_clone_url* is given).

    Returns True if the SHA is available after all attempts, False otherwise.
    """
    if ensure_ref_available(repo_path, sha):
        return True

    # Strategy 2: fetch the named branch from origin.
    if branch:
        logger.debug("SHA %s not local; fetching branch %s from origin", sha[:12], branch)
        fetch_ref(repo_path, branch)
        if ensure_ref_available(repo_path, sha):
            return True

    # Strategy 3: fetch the GitHub PR ref from origin.
    if pr_number > 0:
        pr_ref = f"refs/pull/{pr_number}/head"
        logger.debug("SHA %s not local; fetching PR ref %s from origin", sha[:12], pr_ref)
        fetch_ref(repo_path, pr_ref)
        if ensure_ref_available(repo_path, sha):
            return True

    # Strategy 4: add a fork remote and fetch from it.
    if fork_clone_url:
        remote_name = _ensure_fork_remote(repo_path, fork_clone_url)
        logger.debug(
            "SHA %s not local; fetching from fork remote %s (%s)",
            sha[:12],
            remote_name,
            fork_clone_url,
        )
        try:
            subprocess.run(
                ["git", "fetch", remote_name],
                capture_output=True,
                text=True,
                check=True,
                cwd=str(repo_path),
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            logger.debug("Fork remote fetch failed for remote %s", remote_name)
        if ensure_ref_available(repo_path, sha):
            return True

    logger.debug("SHA %s could not be fetched via any strategy", sha[:12])
    return False
