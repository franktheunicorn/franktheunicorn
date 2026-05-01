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

    Implementations: ``GitHubClient`` (backends/github.py),
    ``ForgejoClient`` (backends/forgejo.py),
    ``MockForgeClient`` (backends/mock.py).
    """

    @abstractmethod
    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]: ...

    @abstractmethod
    def get_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str: ...

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
        - ``comment_ids`` (list[int]): per-inline-comment IDs, in the
          same positional order as ``review.comments``. The poster zips
          this against the in-memory drafts to populate
          ``ReviewDraft.forge_comment_id``. Comments dropped during
          translation (e.g. unlocatable diff position) do NOT contribute
          an entry — the list may be shorter than ``review.comments``.
        """

    @abstractmethod
    def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_issue_comments(
        self, owner: str, repo: str, issue_number: int, since: str | None = None
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def delete_review_comment(self, owner: str, repo: str, pr_number: int, comment_id: int) -> None:
        """Delete a posted review comment.

        ``pr_number`` is required by GitLab (notes are scoped to the MR);
        GitHub and Gitea ignore it.
        """

    @abstractmethod
    def get_authenticated_user(self) -> dict[str, Any]: ...

    @abstractmethod
    def close(self) -> None: ...


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
