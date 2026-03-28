"""Stub review-drafting service.

For v0, this returns deterministic fake comments so the pipeline is
end-to-end testable without an LLM API key.

The interface is designed so that swapping in a real LLM provider later
requires only replacing ``generate_draft`` - the rest of the pipeline
(storage, operator loop, anti-pattern filtering) stays the same.
"""

from __future__ import annotations

import logging
from typing import Protocol

from franktheunicorn.models import PullRequest, ReviewDraft

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider protocol - swap this for a real LLM later.
# ---------------------------------------------------------------------------


class ReviewProvider(Protocol):
    """Interface that any review provider must implement."""

    def generate_draft(
        self,
        pr: PullRequest,
        review_context: str,
        changed_files: list[str],
    ) -> str:
        """Return a draft review comment body as a plain string."""
        ...


# ---------------------------------------------------------------------------
# Stub provider (deterministic, no API calls)
# ---------------------------------------------------------------------------


_STUB_COMMENTS: list[str] = [
    "Thanks for the contribution! A few things worth checking:\n\n"
    "- Make sure all public methods have docstrings.\n"
    "- Run the test suite locally before merging.\n"
    "- Check for any linting warnings.",
    "Looks reasonable at a glance. Could you add a short description of the "
    "motivation in the PR body if it's not already there?",
    "The changed files touch some shared utilities - worth checking that "
    "downstream callers are not affected.",
    "Reminder to check test coverage for the new code paths.",
]


class StubReviewProvider:
    """Returns rotating deterministic stub comments. No LLM required."""

    def __init__(self) -> None:
        self._counter = 0

    def generate_draft(
        self,
        pr: PullRequest,
        review_context: str,
        changed_files: list[str],
    ) -> str:
        comment = _STUB_COMMENTS[self._counter % len(_STUB_COMMENTS)]
        self._counter += 1
        files_note = ""
        if changed_files:
            top5 = ", ".join(changed_files[:5])
            files_note = f"\n\n*Changed files ({len(changed_files)}): {top5}*"
        return f"[DRAFT - stub review]\n\n{comment}{files_note}"


# ---------------------------------------------------------------------------
# Pipeline helper
# ---------------------------------------------------------------------------

# Module-level default provider.  Override in tests or config.
_default_provider: ReviewProvider = StubReviewProvider()


def set_provider(provider: ReviewProvider) -> None:
    """Replace the module-level provider (used in tests / future config)."""
    global _default_provider
    _default_provider = provider


def create_review_draft(
    pr: PullRequest,
    review_context: str = "",
    changed_files: list[str] | None = None,
    provider: ReviewProvider | None = None,
) -> ReviewDraft:
    """Generate a ReviewDraft ORM object (not yet persisted).

    The caller is responsible for adding it to a session and committing.
    """
    resolved_provider = provider or _default_provider
    body = resolved_provider.generate_draft(
        pr=pr,
        review_context=review_context,
        changed_files=changed_files or [],
    )
    draft = ReviewDraft(
        pull_request_id=pr.id,
        source="stub",
        body=body,
        status="pending",
    )
    logger.info("Created draft for PR #%s (source=%s)", pr.github_pr_number, draft.source)
    return draft
