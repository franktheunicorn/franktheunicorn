"""Forge backend abstraction (GitHub, Gitea/Forgejo, GitLab) and the polling/posting glue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.backends.base import (
    ForgeClient,
    ReviewBody,
    ReviewComment,
    infer_username,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import ForgeRegistryEntry

__all__ = [
    "ForgeClient",
    "ReviewBody",
    "ReviewComment",
    "infer_username",
    "make_client",
]


def make_client(entry: ForgeRegistryEntry) -> ForgeClient:
    """Construct the appropriate ForgeClient for a registry entry.

    Gitea and Forgejo share the same underlying API, so both ``type``
    values map to the same client implementation. GitLab and Gitea/Forgejo
    clients are added in subsequent commits — referencing them now would
    fail at import time.
    """
    if entry.type == "github":
        from franktheunicorn.backends.github import GitHubClient

        return GitHubClient(token=entry.token, base_url=entry.base_url)
    if entry.type in ("gitea", "forgejo"):
        from franktheunicorn.backends.gitea import GiteaClient

        return GiteaClient(token=entry.token, base_url=entry.base_url)
    if entry.type == "gitlab":
        from franktheunicorn.backends.gitlab import GitLabClient

        return GitLabClient(token=entry.token, base_url=entry.base_url)
    msg = f"forge type {entry.type!r} is not yet implemented"
    raise NotImplementedError(msg)
