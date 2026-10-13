"""
Forge review posting — batch post approved findings as a single review.

Forge-agnostic: builds ``ReviewBody`` / ``ReviewComment`` dataclasses
and lets each ``ForgeClient`` translate to its own wire format.
Supports suggestion blocks, multi-line comments, attribution footer,
and comment recall within a configurable window.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from franktheunicorn.backends.base import ForgeClient, ReviewBody, ReviewComment
from franktheunicorn.backends.diff_position import translate_line_to_position
from franktheunicorn.backends.mentions import escape_handles, mention_scan, normalize_allowlist
from franktheunicorn.core.models import PullRequest, ReviewDraft

logger = logging.getLogger(__name__)

MANAGED_MARKER = "<!-- franktheunicorn-managed -->"
DEFAULT_ATTRIBUTION = "Generated with assistance of franktheunicorn"
RECALL_WINDOW_HOURS = 24

#: What ``post_review`` does when the PR's head moved since the drafts were
#: made: refuse (the default; the view offers the other two), post as drafted,
#: or post with every inline comment folded into the review body.
STALE_HEAD_MODES: tuple[str, ...] = ("refuse", "post", "body-only")


class StaleHeadError(Exception):
    """The PR has new commits since the drafts were produced.

    Inline line numbers were computed against the old diff; GitHub anchors
    them against the new one, so they land on the wrong lines or are refused
    (422, failing the whole batch). The guard is Apache Magpie's post-time
    "new commits since the draft" check (``tools/pr-management/src/
    pr_management/code_review/cli.py``, ``_guard``, at apache/magpie commit
    dadb315caa8b881fc1d2195bef03223dac25915a) with the same three ways on:
    refresh the review, post anyway, or post the body only.
    """

    def __init__(self, stored: str, live: str) -> None:
        self.stored = stored
        self.live = live
        super().__init__(f"PR head moved {stored[:8]} -> {live[:8]} since the drafts were made")


def _format_suggestion_block(suggestion: str) -> str:
    """Wrap suggestion text in GitHub suggestion markdown."""
    return f"\n```suggestion\n{suggestion}\n```\n"


def _format_comment_body(
    draft: ReviewDraft,
    attribution: str = DEFAULT_ATTRIBUTION,
    allowed_mentions: frozenset[str] = frozenset(),
) -> str:
    """Build the full comment body with suggestion block and attribution.

    Every ``@handle`` outside code is backtick-quoted first (see
    ``backends/mentions.py``): the review notifies the author and the
    subscribers already, and a handle a model wrote or quoted from the PR
    was nobody's decision to ping.
    """
    body = draft.edited_body if draft.edited_body else draft.comment_body
    body = escape_handles(body, allowed_mentions)

    if draft.suggestion:
        body += _format_suggestion_block(draft.suggestion)

    body += f"\n\n---\n<sub>{attribution}</sub>\n{MANAGED_MARKER}"
    return body


def _build_review_comment(
    draft: ReviewDraft, attribution: str, allowed_mentions: frozenset[str] = frozenset()
) -> ReviewComment:
    """Build a normalized ReviewComment from a stored draft."""
    return ReviewComment(
        path=draft.file_path,
        body=_format_comment_body(draft, attribution, allowed_mentions),
        correlation_key=str(draft.pk),
        line=draft.line_number if draft.line_number else None,
        line_end=draft.line_end if draft.line_end else None,
    )


def _is_anchored(comment: ReviewComment, diff_text: str) -> bool:
    """Does the current diff show the line(s) this inline comment points at?"""
    if comment.line is None:
        return True
    if translate_line_to_position(diff_text, comment.path, comment.line, comment.side) is None:
        return False
    return (
        comment.line_end is None
        or translate_line_to_position(diff_text, comment.path, comment.line_end, comment.side)
        is not None
    )


def _folded(comment: ReviewComment, reason: str) -> ReviewComment:
    """The same comment as a file-level one, which the forge client puts in
    the review body: the finding is kept, just not pinned to a line the
    forge would refuse. Magpie's ``inline_comments`` does the same with its
    unanchorable findings (``code_review/body.py`` at the pinned commit)."""
    where = f"`{comment.path}`" if comment.path else "this PR"
    if comment.line is not None:
        where += f" line {comment.line}"
        if comment.line_end is not None and comment.line_end > comment.line:
            where += f"-{comment.line_end}"
    return ReviewComment(
        path=comment.path,
        body=f"_(on {where}, {reason})_\n\n{comment.body}",
        correlation_key=comment.correlation_key,
        line=None,
        line_end=None,
        side=comment.side,
    )


def _same_sha(a: str, b: str) -> bool:
    """Prefix-tolerant: a stored sha may be the short form."""
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


class GitHubPoster:
    """Posts review findings to a forge as a single batch review.

    The class name is kept for back-compat with existing imports; it now
    works with any ``ForgeClient`` (GitHub, Forgejo, mock, ...).
    """

    def __init__(
        self,
        client: ForgeClient,
        attribution: str = DEFAULT_ATTRIBUTION,
        mention_allowlist: Iterable[str] = (),
    ) -> None:
        self._client = client
        self._attribution = attribution
        self._allowed_mentions = normalize_allowlist(mention_allowlist)

    def post_review(
        self,
        pr: PullRequest,
        drafts: list[ReviewDraft] | None = None,
        event: str = "COMMENT",
        *,
        on_stale_head: str = "refuse",
    ) -> dict[str, Any] | None:
        """Post approved drafts as a single review on the project's forge.

        Returns the forge's response dict, or None if there's nothing to
        post. The response is expected to include
        ``comment_ids_by_key: dict[str, int]`` populated by the
        underlying ``ForgeClient.create_review`` (see ``ForgeClient``
        docs); IDs are matched deterministically by correlation key. Two
        keys are added here: ``mentions_escaped`` (the handles that were
        backtick-quoted) and ``folded_into_body`` (inline comments whose
        line the current diff no longer shows, posted as part of the review
        body instead).

        Before anything is sent the PR's live head is compared with the one
        the drafts were made against (``pr.head_sha``); if it moved, the
        call raises :class:`StaleHeadError` unless ``on_stale_head`` is
        ``"post"`` (as drafted) or ``"body-only"`` (every inline comment
        folded). Either way each inline comment is then checked against the
        current diff and folded when its line is not in it, which is what
        GitHub would otherwise reject with a 422 for the whole batch.
        """
        if on_stale_head not in STALE_HEAD_MODES:
            msg = f"on_stale_head must be one of {STALE_HEAD_MODES}, got {on_stale_head!r}"
            raise ValueError(msg)
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

        live_head = self._live_head_sha(pr)
        if live_head and pr.head_sha and not _same_sha(live_head, pr.head_sha):
            if on_stale_head == "refuse":
                logger.info(
                    "Not posting review for PR #%d: head moved %s -> %s since the drafts "
                    "were made (the operator can refresh, post anyway, or post body-only)",
                    pr.number,
                    pr.head_sha[:8],
                    live_head[:8],
                )
                raise StaleHeadError(pr.head_sha, live_head)
            logger.info(
                "Posting review for PR #%d over a moved head (%s -> %s), mode=%s",
                pr.number,
                pr.head_sha[:8],
                live_head[:8],
                on_stale_head,
            )
        elif not live_head:
            logger.info(
                "Could not read the live head of PR #%d; posting without the moved-head check",
                pr.number,
            )

        mentions_escaped: list[str] = []
        for draft in drafts:
            raw = draft.edited_body if draft.edited_body else draft.comment_body
            mentions_escaped.extend(h.handle for h in mention_scan(raw, self._allowed_mentions))
        if mentions_escaped:
            logger.info(
                "Backtick-quoted %d live @-mention(s) in the review for PR #%d: %s",
                len(mentions_escaped),
                pr.number,
                ", ".join(sorted(set(mentions_escaped))),
            )

        comments = [
            _build_review_comment(d, self._attribution, self._allowed_mentions) for d in drafts
        ]
        folded = 0
        if on_stale_head == "body-only":
            comments = [
                _folded(c, "posted body-only because the PR has new commits") for c in comments
            ]
            folded = sum(1 for d in drafts if d.line_number)
        else:
            diff_text = self._current_diff(pr)
            if diff_text:
                checked: list[ReviewComment] = []
                for c in comments:
                    if _is_anchored(c, diff_text):
                        checked.append(c)
                    else:
                        folded += 1
                        checked.append(_folded(c, "which the current diff does not show"))
                comments = checked
        if folded:
            logger.info(
                "Folded %d inline comment(s) into the review body for PR #%d", folded, pr.number
            )
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
        if isinstance(result, dict):
            result["mentions_escaped"] = sorted(set(mentions_escaped))
            result["folded_into_body"] = folded

        for draft in drafts:
            draft.status = "posted"
            draft.posted_at = now
            cid = comment_ids_by_key.get(str(draft.pk))
            if cid is not None:
                draft.forge_comment_id = cid
            draft.save(update_fields=["status", "posted_at", "forge_comment_id", "updated_at"])

        return result

    def _live_head_sha(self, pr: PullRequest) -> str:
        """The PR's head sha as the forge sees it now; "" when it cannot be read.

        Defensive about the payload: ``get_pull_request`` returns the forge's
        own dict and only GitHub and Gitea are known to put it at
        ``head.sha``. An unreadable head skips the guard (logged by the
        caller) rather than blocking the post.
        """
        try:
            data = self._client.get_pull_request(pr.project.owner, pr.project.repo, pr.number)
        except Exception:
            logger.warning(
                "Could not fetch PR #%d to check its head before posting", pr.number, exc_info=True
            )
            return ""
        head = data.get("head") if isinstance(data, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        return sha if isinstance(sha, str) else ""

    def _current_diff(self, pr: PullRequest) -> str:
        """The PR's diff as the forge serves it now; "" when it cannot be read
        (then nothing is folded and the post goes out as drafted, at WARNING)."""
        try:
            diff = self._client.get_pull_request_diff(pr.project.owner, pr.project.repo, pr.number)
        except Exception:
            logger.warning(
                "Could not fetch the current diff of PR #%d; posting inline comments "
                "without anchor validation",
                pr.number,
                exc_info=True,
            )
            return ""
        return diff if isinstance(diff, str) else ""

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
