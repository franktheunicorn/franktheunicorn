"""Forge backend abstraction (GitHub, Gitea/Forgejo, GitLab) and the polling/posting glue."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from franktheunicorn.backends.base import (
    ForgeClient,
    ReviewBody,
    ReviewComment,
    infer_username,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import ForgeRegistryEntry
    from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

logger = logging.getLogger(__name__)

__all__ = [
    "ForgeClient",
    "ReviewBody",
    "ReviewComment",
    "infer_username",
    "make_client",
]


@lru_cache(maxsize=8)
def _rate_limiter_for(db_path: str) -> GitHubRateLimiter | None:
    """One limiter per bucket path, reused for the life of the process.

    Cached deliberately, for two reasons. Each instance opens a SQLite
    connection it never closes, and ``ingest_single_pr`` builds a client per PR
    — up to 100 per cycle from the mention scan — so a fresh limiter each time
    leaked a connection each time. And the header-derived remaining/reset pair
    that the limiter calls its authoritative brake lives in plain instance
    attributes: a new instance starts out blind to quota it has already spent.
    """
    try:
        from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

        return GitHubRateLimiter(db_path)
    except Exception:
        logger.debug("Could not initialize GitHub rate limiter", exc_info=True)
        return None


def _github_rate_limiter() -> GitHubRateLimiter | None:
    """The process-wide adaptive limiter, or None when there's no data dir.

    Same SQLite bucket the data_access fetchers use, so ingestion and the
    contextual fetchers pace against one budget instead of two.
    """
    try:
        from pathlib import Path

        from django.conf import settings

        return _rate_limiter_for(str(Path(settings.DATA_DIR) / "rate_limits.sqlite"))
    except Exception:
        logger.debug("Could not resolve the rate-limiter bucket path", exc_info=True)
        return None


def make_client(entry: ForgeRegistryEntry) -> ForgeClient:
    """Construct the appropriate ForgeClient for a registry entry.

    Gitea and Forgejo share the same underlying API, so both ``type``
    values map to the same ``GiteaClient`` implementation. Raises
    ``NotImplementedError`` for unrecognized forge types.
    """
    if entry.type == "github":
        from franktheunicorn.backends.github import GitHubClient

        return GitHubClient(
            token=entry.token,
            base_url=entry.base_url,
            rate_limiter=_github_rate_limiter(),
        )
    if entry.type in ("gitea", "forgejo"):
        from franktheunicorn.backends.gitea import GiteaClient

        return GiteaClient(token=entry.token, base_url=entry.base_url)
    if entry.type == "gitlab":
        from franktheunicorn.backends.gitlab import GitLabClient

        return GitLabClient(token=entry.token, base_url=entry.base_url)
    msg = f"forge type {entry.type!r} is not yet implemented"
    raise NotImplementedError(msg)
