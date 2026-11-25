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
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Upper bound on how many open PRs one ``list_pull_requests`` call returns.
# Implementations stop paginating here; callers that infer "this PR is gone
# from the forge" from a listing must treat a result of this size as possibly
# truncated rather than complete.
MAX_LISTED_PULL_REQUESTS = 1000


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


@dataclass
class UpdatedPullRequests:
    """PRs of any state that changed since a watermark, most recent first.

    ``complete`` is False when the walk stopped at its page limit before
    reaching the watermark: more changed than one call is allowed to read, and
    the rest is left to the cycle's full poll.
    """

    items: list[dict[str, Any]]
    complete: bool


@dataclass
class NotificationPage:
    """The token owner's PR notifications since a watermark.

    ``poll_interval`` is the forge's own ``X-Poll-Interval`` (seconds), which it
    asks clients to respect; 0 when it said nothing.
    """

    items: list[dict[str, Any]]
    poll_interval: int = 0
    #: False when more arrived than one read takes: the oldest are not in
    #: ``items``, and the caller should say what covers them instead.
    complete: bool = True


class NotificationsUnavailableError(Exception):
    """The token cannot read notifications — not a transient failure.

    GitHub refuses ``/notifications`` to fine-grained personal access tokens and
    to GitHub App tokens, and a classic token needs the ``notifications`` (or
    ``repo``) scope. Retrying every minute would change nothing, so callers
    fall back to searching and say so once.
    """

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        super().__init__(f"HTTP {status}: {message}" if message else f"HTTP {status}")


class ForgeClient(ABC):
    """Abstract client for a code-forge REST API.

    Implementations:
    - ``GitHubClient`` (``backends/github.py``)
    - ``GiteaClient`` (``backends/gitea.py``) — also serves Forgejo via
      the shared Gitea-compatible API
    - ``GitLabClient`` (``backends/gitlab.py``)
    - ``MockForgeClient`` (``backends/mock.py``)
    """

    # True only when ``list_pull_requests`` paginates to exhaustion, so an
    # absent PR really means "not open on the forge any more" rather than
    # "on a page we never asked for". The poller's closed-PR reap requires
    # this; a client that returns one short page (Gitea and GitLab both stop
    # at 50) would otherwise have every PR past that page marked closed.
    # Flip it on once the implementation paginates.
    lists_all_open_pull_requests: bool = False

    @abstractmethod
    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        pass

    def list_pull_requests_page(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "closed",
        page: int = 1,
        per_page: int = 100,
        sort: str = "created",
    ) -> list[dict[str, Any]]:
        """One page of PRs in *state*, newest first by *sort* (``created`` or
        ``updated``).

        The paginated sibling of :meth:`list_pull_requests`, for callers that
        walk history rather than snapshot the open set — the zombie scan reads
        a project's closed PRs a page at a time across many runs, and needs to
        say which page. ``created`` descending means PR numbers descend too,
        which is what makes a stored frontier number a cursor; ``updated``
        descending is how the same scan catches what closed since it last ran.

        Only GitHub implements it today; the default raises so a caller can
        degrade (log and skip the project) rather than silently see nothing.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support list_pull_requests_page()"
        )

    #: True when :meth:`list_pull_requests_updated_since` is implemented. The
    #: watcher reads it to pick a project up; the mid-cycle full-listing sync
    #: covers the projects whose forge can't answer "what changed since?".
    supports_updated_since: bool = False

    def list_pull_requests_updated_since(
        self, owner: str, repo: str, since: datetime | None, *, max_pages: int = 10
    ) -> UpdatedPullRequests:
        """Open, closed and merged PRs whose ``updated_at`` is at or after *since*.

        Most recently updated first. A PR being opened, closed, merged, pushed to,
        relabelled or commented on all move its ``updated_at``, so this one
        listing answers all of "what's new", "what closed" and "what moved" — in
        one call when little did. ``since=None`` reads only the first page.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support list_pull_requests_updated_since()"
        )

    def list_notifications(self, since: datetime | None) -> NotificationPage:
        """The authenticated user's pull-request notifications updated since *since*.

        Read-only: listing does not mark anything read, so the operator's inbox
        is left as they had it. Raises :class:`NotificationsUnavailableError`
        when the token can't read notifications at all.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support list_notifications()")

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

    def list_pull_request_reviews(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """Every submitted review on the PR, GitHub-shaped.

        Each dict carries ``user.login``, ``state`` (``APPROVED``,
        ``CHANGES_REQUESTED``, ``COMMENTED``, ``DISMISSED``), ``body``,
        ``submitted_at`` (ISO 8601, may be empty) and ``commit_id`` (may be
        empty). The intending-to-merge detector and the almost-ready queue
        read these; forges without a reviews API inherit this empty default
        and see no approvals rather than erroring.
        """
        return []

    def get_commit_ci_status(self, owner: str, repo: str, sha: str) -> str:
        """Fold the commit's CI into ``pass`` / ``fail`` / ``pending`` / ``""``.

        ``""`` is unknown: no checks reported, or a forge that does not
        expose them (this default). Read for the intending-to-merge queue,
        where "approved pending CI" needs the answer, and for the merge
        queue's eligibility check.
        """
        return ""

    @abstractmethod
    def delete_review_comment(self, owner: str, repo: str, pr_number: int, comment_id: int) -> None:
        """Delete a posted review comment.

        ``pr_number`` is required by GitLab (notes are scoped to the MR);
        GitHub and Gitea ignore it.
        """

    def list_repo_collaborators(self, owner: str, repo: str, permission: str = "push") -> list[str]:
        """Logins with at least *permission* on the repository.

        Read once at worker boot to learn a project's committers
        (``ProjectConfig.committer_repos``). Forges without the call inherit
        this and raise ``NotImplementedError``; errors from a forge that has
        it propagate, so the caller can keep the last list it stored.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot list repository collaborators")

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        """Logins in the org's team *team_slug* (``ProjectConfig.committer_teams``).

        Same contract as ``list_repo_collaborators``: ``NotImplementedError``
        where the forge has no teams, errors propagate otherwise.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot list team members")

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

    def get_commit_checks(self, owner: str, repo: str, sha: str) -> list[dict[str, Any]]:
        """Return CI results for a commit, normalised to one shape.

        Each entry is ``{"name", "status", "conclusion", "url"}`` where
        ``status`` is ``completed`` / ``in_progress`` / ``queued`` and
        ``conclusion`` is ``success`` / ``failure`` / ``timed_out`` /
        ``cancelled`` / ``neutral`` / ``skipped`` / ``action_required`` or
        ``""`` while running. Check runs and plain commit statuses both land
        here. The default raises ``NotImplementedError``; the almost-ready
        queue then runs without CI signals.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support get_commit_checks()")

    def get_commit_check_failures(self, owner: str, repo: str, sha: str) -> list[dict[str, Any]]:
        """What failed on a commit: one entry per failed job, with a log excerpt.

        Each entry is ``{"check", "job", "step", "conclusion", "url", "title",
        "summary", "log", "linked_run"}`` — see ``GitHubClient`` for the shape
        and for why a check and a job are not the same thing. The default raises
        ``NotImplementedError``; CI-failure classification (review/ci_failure.py)
        then works from the check names ``get_commit_checks`` gave it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support get_commit_check_failures()"
        )

    def create_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict[str, Any]:
        """Post a conversation comment (not a review) on a PR.

        Used by the "ask the author to rebase" button. The default raises
        ``NotImplementedError``; callers fall back to a body-only
        ``create_review`` with ``event="COMMENT"``, which every forge supports.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support create_issue_comment()")

    def search_prs_involving(self, username: str, max_results: int = 100) -> list[dict[str, Any]]:
        """Search for open PRs where ``username`` is involved (mention/assign/review-request).

        Default implementation returns an empty list. Forge clients that support
        a search API (currently only GitHub) should override this.
        """
        return []

    def search_prs_authored_by(self, username: str, max_results: int = 100) -> list[dict[str, Any]]:
        """Search for open PRs ``username`` wrote.

        Separate from :meth:`search_prs_involving` even though ``involves:``
        nominally covers authorship, because the two compete for the same result
        budget and lose differently. "Involved" is the noisy set — every thread
        the operator ever commented on — and a single relevance-ranked page of it
        can push the operator's *own* PRs out entirely, which is exactly the set
        the shepherding pass, the merge queue and the "Your PRs" queue are built
        on. Asking for them by name costs one more search call and makes them
        unmissable.

        Default implementation returns an empty list; forges with a search API
        should override.
        """
        return []

    def search_prs_reviewed_by(self, username: str, max_results: int = 100) -> list[dict[str, Any]]:
        """Search for open PRs ``username`` has already submitted a review on.

        A third distinct set, not a subset of the other two. ``involves:`` covers
        commenters, and a review submitted *without* a comment body — a plain
        Approve, a Request-changes with only inline notes — need not make you one;
        ``author:`` obviously doesn't cover it either. These are the PRs the
        operator has already spent judgement on and is on the hook to re-check when
        the author pushes again, which makes them the last set that should go
        missing.

        Default implementation returns an empty list; forges with a search API
        should override.
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
