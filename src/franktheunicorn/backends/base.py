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
    correlation_key: str = ""
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

    def get_commit_diff(self, owner: str, repo: str, sha: str) -> str:
        """Return the unified diff for a single commit.

        Used by the backport check to fetch the source diff when a PR
        declares it cherry-picks a specific commit SHA. Only GitHub
        implements this today; other forges inherit this default and raise
        ``NotImplementedError`` so callers degrade gracefully (the backport
        check turns the failure into a single informational finding).
        """
        raise NotImplementedError(f"{type(self).__name__} does not support get_commit_diff()")

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
        - ``comment_ids_by_key`` (dict[str, int]): mapping from each
          inline comment's client-provided ``ReviewComment.correlation_key``
          to the forge comment ID returned for that exact comment.
          Comments dropped by the forge or missing IDs are omitted.
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
    def list_contributors(self, owner: str, repo: str) -> list[str]:
        """Return login names of contributors to the repository.

        Implementations should return at least the top contributors. Order
        and completeness are best-effort — the result is used only for
        new-contributor detection, so false negatives (missing someone) are
        acceptable but should be minimised.
        """

    @abstractmethod
    def get_authenticated_user(self) -> dict[str, Any]:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    def search_prs_involving(self, username: str, max_results: int = 100) -> list[dict[str, Any]]:
        """Search for open PRs where ``username`` is involved (mention/assign/review-request).

        Default implementation returns an empty list. Forge clients that support
        a search API (currently only GitHub) should override this.
        """
        return []


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
