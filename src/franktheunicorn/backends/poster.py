"""
Forge review posting — batch post approved findings as a single review.

Forge-agnostic: builds ``ReviewBody`` / ``ReviewComment`` dataclasses
and lets each ``ForgeClient`` translate to its own wire format.
Supports suggestion blocks, multi-line comments, attribution footer,
and comment recall within a configurable window.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from franktheunicorn.backends.base import ForgeClient, ReviewBody, ReviewComment
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


def _build_review_comment(draft: ReviewDraft, attribution: str) -> ReviewComment:
    """Build a normalized ReviewComment from a stored draft."""
    return ReviewComment(
        path=draft.file_path,
        body=_format_comment_body(draft, attribution),
        correlation_key=str(draft.pk),
        line=draft.line_number if draft.line_number else None,
        line_end=draft.line_end if draft.line_end else None,
    )


class GitHubPoster:
    """Posts review findings to a forge as a single batch review.

    The class name is kept for back-compat with existing imports; it now
    works with any ``ForgeClient`` (GitHub, Forgejo, mock, ...).
    """

    def __init__(self, client: ForgeClient, attribution: str = DEFAULT_ATTRIBUTION) -> None:
        self._client = client
        self._attribution = attribution

    def post_review(
        self,
        pr: PullRequest,
        drafts: list[ReviewDraft] | None = None,
        event: str = "COMMENT",
    ) -> dict[str, Any] | None:
        """Post approved drafts as a single review on the project's forge.

        Returns the forge's response dict, or None if there's nothing to
        post. The response is expected to include
        ``comment_ids_by_key: dict[str, int]`` populated by the
        underlying ``ForgeClient.create_review`` (see ``ForgeClient``
        docs); IDs are matched deterministically by correlation key.
        """
        if drafts is None:
            # Edited drafts are postable too — the operator's rewrite is the
            # strongest approval signal (``_format_comment_body`` prefers
            # ``edited_body``); without this they'd be stranded unpostable.
            drafts = list(
                ReviewDraft.objects.filter(
                    pull_request=pr,
                    status__in=["accepted", "edited"],
                ).order_by("file_path", "line_number")
            )

        if not drafts:
            return None

        comments = [_build_review_comment(d, self._attribution) for d in drafts]
        review = ReviewBody(event=event, comments=comments)

        try:
            result: dict[str, Any] = self._client.create_review(
                pr.project.owner,
                pr.project.repo,
                pr.number,
                review,
            )
        except Exception:
            logger.exception("Failed to post review for PR #%d", pr.number)
            return None

        now = datetime.now(tz=UTC)

        # Per-comment IDs come back on the create_review response in 1:1
        # alignment with the comments we submitted. ``None`` entries flag
        # comments that were dropped during translation (e.g. unlocatable
        # diff position on Gitea, missing MR refs on GitLab) — for those
        # drafts we mark ``posted`` but leave ``forge_comment_id`` unset,
        # so a later recall doesn't accidentally delete a sibling
        # draft's comment.
        raw_comment_ids = result.get("comment_ids_by_key") if result else {}
        comment_ids_by_key: dict[str, int] = (
            raw_comment_ids if isinstance(raw_comment_ids, dict) else {}
        )

        for draft in drafts:
            draft.status = "posted"
            draft.posted_at = now
            cid = comment_ids_by_key.get(str(draft.pk))
            if cid is not None:
                draft.forge_comment_id = cid
            draft.save(update_fields=["status", "posted_at", "forge_comment_id", "updated_at"])

        return result

    def recall_comment(self, draft: ReviewDraft) -> bool:
        """Delete a posted comment if within the recall window.

        Returns True if the comment was successfully recalled.
        """
        if not draft.forge_comment_id:
            logger.warning("Cannot recall draft %d: no forge_comment_id", draft.pk)
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
                draft.pull_request.number,
                draft.forge_comment_id,
            )
        except Exception:
            logger.exception("Failed to recall comment %d", draft.forge_comment_id)
            return False

        draft.status = "recalled"
        draft.save(update_fields=["status", "updated_at"])
        return True
