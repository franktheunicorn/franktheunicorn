"""
Forge-agnostic client abstraction.

Defines the ABC that all forge backends (GitHub, Forgejo/Gitea, ...) implement,
plus the normalized review payload dataclasses used by the poster. Wire-format
differences between forges are confined to each subclass's ``create_review``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ReviewComment:
    """A single inline review comment, forge-agnostic.

    ``line`` is the line number in the diff's RIGHT (added) side by default,
    matching how operators think about it. ``line_end`` makes the comment
    span a range. Each ForgeClient subclass converts to its wire format.
    """

    path: str
    body: str
    line: int | None = None
    line_end: int | None = None
    side: str = "RIGHT"

    def __post_init__(self) -> None:
        if self.line_end is not None and self.line is not None and self.line_end < self.line:
            msg = f"ReviewComment.line_end ({self.line_end}) must be >= line ({self.line})"
            raise ValueError(msg)


@dataclass
class ReviewBody:
    """A normalized review payload submitted via ``create_review``."""

    event: str = "COMMENT"
    body: str = ""
    comments: list[ReviewComment] = field(default_factory=list)


class ForgeClient(ABC):
    """Abstract client for a code-forge REST API.

    Implementations:
    - ``GitHubClient`` (``backends/github.py``)
    - ``GiteaClient`` (``backends/gitea.py``) — also serves Forgejo via
      the shared Gitea-compatible API
    - ``GitLabClient`` (``backends/gitlab.py``)
    - ``MockForgeClient`` (``backends/mock.py``)
    """

    @abstractmethod
    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        pass

    @abstractmethod
    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        pass

    @abstractmethod
    def create_review(
        self, owner: str, repo: str, pr_number: int, review: ReviewBody
    ) -> dict[str, Any]:
        """Submit a review payload and return the forge's response.

        Implementations MUST populate two keys on the returned dict:

        - ``id`` (int | None): the forge's identifier for the review or
          (on GitLab) the body note. Used by ``GitHubPoster`` for
          tracking; can be ``None`` if neither body nor comments produced
          a top-level identifier.
        - ``comment_ids`` (list[int | None]): per-inline-comment IDs in
          1:1 positional alignment with ``review.comments``. ``None`` at
          position *i* means the comment at ``review.comments[i]`` was
          dropped (e.g. unlocatable diff position on Gitea/Forgejo,
          missing MR refs on GitLab) or its ID could not be retrieved.
          The poster zips this against the in-memory drafts to populate
          ``ReviewDraft.forge_comment_id``; entries set to ``None`` skip
          the assignment so each draft only ever stores its own ID.
        """

    @abstractmethod
    def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def get_issue_comments(
        self, owner: str, repo: str, issue_number: int, since: str | None = None
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def delete_review_comment(self, owner: str, repo: str, pr_number: int, comment_id: int) -> None:
        """Delete a posted review comment.

        ``pr_number`` is required by GitLab (notes are scoped to the MR);
        GitHub and Gitea ignore it.
        """

    @abstractmethod
    def get_authenticated_user(self) -> dict[str, Any]:
        pass

    @abstractmethod
    def close(self) -> None:
        pass


def infer_username(client: ForgeClient) -> str:
    """Infer the authenticated user's login from any ForgeClient.

    Returns an empty string on any error (network, invalid token, missing
    field). Both GitHub and Gitea/Forgejo return ``login`` on the
    ``/user`` endpoint, so this works uniformly.
    """
    try:
        user_data = client.get_authenticated_user()
        login = user_data.get("login", "")
        if not isinstance(login, str):
            return ""
        if login:
            logger.info("Inferred forge username from token: %s", login)
        return login
    except Exception:
        logger.warning(
            "Could not infer forge username from token (network error or invalid token)",
            exc_info=True,
        )
        return ""
