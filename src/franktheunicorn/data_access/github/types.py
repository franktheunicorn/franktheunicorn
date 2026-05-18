"""Typed dataclasses for GitHub data (immutable DTOs).

These are the data-transfer objects returned by the dual-path fetchers.
They are intentionally separate from Django ORM models.
"""

from __future__ import annotations

from dataclasses import dataclass

from franktheunicorn.data_access.base import FetchResult


@dataclass(frozen=True)
class PRFileChange:
    """A single file changed in a pull request."""

    filename: str
    status: str  # "modified", "added", "removed", "renamed"
    additions: int = 0
    deletions: int = 0
    patch: str = ""


@dataclass(frozen=True)
class PRSummary(FetchResult):
    """Summary of a single pull request."""

    number: int = 0
    title: str = ""
    author: str = ""
    state: str = "open"
    url: str = ""
    diff_url: str = ""
    body: str = ""
    labels: tuple[str, ...] = ()
    requested_reviewers: tuple[str, ...] = ()
    is_draft: bool = False
    created_at: str = ""
    updated_at: str = ""
    additions: int = 0
    deletions: int = 0
    files: tuple[PRFileChange, ...] = ()
    mergeable: bool | None = None


@dataclass(frozen=True)
class PRDiff(FetchResult):
    """Full diff for a pull request."""

    pr_number: int = 0
    raw_diff: str = ""
    files: tuple[PRFileChange, ...] = ()


@dataclass(frozen=True)
class ReviewComment:
    """An inline comment within a review."""

    id: int = 0
    author: str = ""
    body: str = ""
    path: str = ""
    line: int | None = None
    created_at: str = ""


@dataclass(frozen=True)
class SingleReview:
    """One review on a pull request."""

    id: int = 0
    author: str = ""
    state: str = ""  # "APPROVED", "CHANGES_REQUESTED", "COMMENTED", etc.
    body: str = ""
    submitted_at: str = ""
    comments: tuple[ReviewComment, ...] = ()


@dataclass(frozen=True)
class PRReview(FetchResult):
    """All reviews for a pull request."""

    pr_number: int = 0
    reviews: tuple[SingleReview, ...] = ()


@dataclass(frozen=True)
class PRTemplateSummary(FetchResult):
    """The PR description template for a repository, if one exists."""

    text: str = ""
