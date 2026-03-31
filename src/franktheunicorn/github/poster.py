"""
GitHub review posting — batch post approved findings as a single review.

Supports suggestion blocks, multi-line comments, attribution footer,
and comment recall within a configurable window.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from franktheunicorn.core.models import PullRequest, ReviewDraft

logger = logging.getLogger(__name__)

MANAGED_MARKER = "<!-- franktheunicorn-managed -->"
DEFAULT_ATTRIBUTION = "Generated with assistance of franktheunicorn"
RECALL_WINDOW_HOURS = 24


def _format_suggestion_block(suggestion: str) -> str:
    """Wrap suggestion text in GitHub suggestion markdown."""
    return f"\n```suggestion\n{suggestion}\n```\n"


def _format_comment_body(
    draft: ReviewDraft,
    attribution: str = DEFAULT_ATTRIBUTION,
) -> str:
    """Build the full comment body with suggestion block and attribution."""
    body = draft.edited_body if draft.edited_body else draft.comment_body

    if draft.suggestion:
        body += _format_suggestion_block(draft.suggestion)

    body += f"\n\n---\n<sub>{attribution}</sub>\n{MANAGED_MARKER}"
    return body


def _build_review_comment(draft: ReviewDraft, attribution: str) -> dict[str, Any]:
    """Build a single review comment dict for the GitHub Reviews API."""
    comment: dict[str, Any] = {
        "path": draft.file_path,
        "body": _format_comment_body(draft, attribution),
    }

    if draft.line_number:
        comment["line"] = draft.line_number
        if draft.line_end and draft.line_end > draft.line_number:
            comment["start_line"] = draft.line_number
            comment["line"] = draft.line_end

    return comment


class GitHubPoster:
    """Posts review findings to GitHub as a single batch review."""

    def __init__(self, client: Any, attribution: str = DEFAULT_ATTRIBUTION) -> None:
        self._client = client
        self._attribution = attribution

    def post_review(
        self,
        pr: PullRequest,
        drafts: list[ReviewDraft] | None = None,
        event: str = "COMMENT",
    ) -> dict[str, Any] | None:
        """Post approved drafts as a single GitHub review.

        Returns the GitHub API response dict, or None if there's nothing to post.
        """
        if drafts is None:
            drafts = list(
                ReviewDraft.objects.filter(
                    pull_request=pr,
                    status="accepted",
                ).order_by("file_path", "line_number")
            )

        if not drafts:
            return None

        comments = [_build_review_comment(d, self._attribution) for d in drafts]

        body = {
            "event": event,
            "comments": comments,
        }

        try:
            result = self._client.create_review(
                pr.project.owner,
                pr.project.repo,
                pr.number,
                body,
            )
        except Exception:
            logger.exception("Failed to post review for PR #%d", pr.number)
            return None

        now = datetime.now(tz=UTC)
        review_id = result.get("id") if result else None

        # Fetch the posted comment IDs from the review.
        comment_ids: list[int] = []
        if review_id:
            try:
                review_comments = self._client.get_review_comments(
                    pr.project.owner,
                    pr.project.repo,
                    pr.number,
                    review_id,
                )
                comment_ids = [c.get("id", 0) for c in review_comments]
            except Exception:
                logger.debug("Could not fetch review comment IDs", exc_info=True)

        for i, draft in enumerate(drafts):
            draft.status = "posted"
            draft.posted_at = now
            if i < len(comment_ids):
                draft.github_comment_id = comment_ids[i]
            draft.save(update_fields=["status", "posted_at", "github_comment_id", "updated_at"])

        return result

    def recall_comment(self, draft: ReviewDraft) -> bool:
        """Delete a posted comment if within the recall window.

        Returns True if the comment was successfully recalled.
        """
        if not draft.github_comment_id:
            logger.warning("Cannot recall draft %d: no github_comment_id", draft.pk)
            return False

        if not draft.posted_at:
            logger.warning("Cannot recall draft %d: no posted_at timestamp", draft.pk)
            return False

        window = timedelta(hours=RECALL_WINDOW_HOURS)
        if datetime.now(tz=UTC) - draft.posted_at > window:
            logger.warning(
                "Cannot recall draft %d: posted %s ago (window: %dh)",
                draft.pk,
                datetime.now(tz=UTC) - draft.posted_at,
                RECALL_WINDOW_HOURS,
            )
            return False

        try:
            self._client.delete_review_comment(
                draft.pull_request.project.owner,
                draft.pull_request.project.repo,
                draft.github_comment_id,
            )
        except Exception:
            logger.exception("Failed to recall comment %d", draft.github_comment_id)
            return False

        draft.status = "recalled"
        draft.save(update_fields=["status", "updated_at"])
        return True
