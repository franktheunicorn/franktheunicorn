"""Forge backend abstraction (GitHub, Gitea/Forgejo, GitLab) and the polling/posting glue."""

from __future__ import annotations

import logging
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


_RATE_LIMITERS: dict[str, GitHubRateLimiter] = {}


def _rate_limiter_for(db_path: str) -> GitHubRateLimiter | None:
    """One limiter per bucket path, reused for the life of the process.

    Cached deliberately, for two reasons. Each instance opens a SQLite
    connection it never closes, and ``ingest_single_pr`` builds a client per PR
    — up to 100 per cycle from the mention scan — so a fresh limiter each time
    leaked a connection each time. And the header-derived remaining/reset pair
    that the limiter calls its authoritative brake lives in plain instance
    attributes: a new instance starts out blind to quota it has already spent.

    Successes only: an ``lru_cache`` here would memoize the ``None`` from one
    transiently locked bucket file and leave the process unpaced until restart,
    which is the failure this pacing exists to prevent.
    """
    cached = _RATE_LIMITERS.get(db_path)
    if cached is not None:
        return cached

    try:
        from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

        limiter = GitHubRateLimiter(db_path)
    except Exception:
        logger.debug("Could not initialize GitHub rate limiter", exc_info=True)
        return None

    _RATE_LIMITERS[db_path] = limiter
    return limiter


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


def make_client(entry: ForgeRegistryEntry, *, pace_requests: bool = True) -> ForgeClient:
    """Construct the appropriate ForgeClient for a registry entry.

    Gitea and Forgejo share the same underlying API, so both ``type``
    values map to the same ``GiteaClient`` implementation. Raises
    ``NotImplementedError`` for unrecognized forge types.

    Pass ``pace_requests=False`` from a web request: the rate limiter's brake is
    a blocking sleep, which is right for the worker and wrong for a view. Quota
    tracking stays on either way.
    """
    if entry.type == "github":
        from franktheunicorn.backends.github import GitHubClient

        return GitHubClient(
            token=entry.token,
            base_url=entry.base_url,
            rate_limiter=_github_rate_limiter(),
            pace_requests=pace_requests,
        )
    if entry.type in ("gitea", "forgejo"):
        from franktheunicorn.backends.gitea import GiteaClient

        return GiteaClient(token=entry.token, base_url=entry.base_url)
    if entry.type == "gitlab":
        from franktheunicorn.backends.gitlab import GitLabClient

        return GitLabClient(token=entry.token, base_url=entry.base_url)
    msg = f"forge type {entry.type!r} is not yet implemented"
    raise NotImplementedError(msg)
