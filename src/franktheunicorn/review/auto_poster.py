"""Confidence-gated auto-posting (v1.5 triple gate).

Posts high-confidence findings automatically using a separate bot identity.
Disabled by default (posting.mode must be "confidence-gated").

Triple gate:
  Gate 1: posting.mode == "confidence-gated" in project config
  Gate 2: finding.confidence >= threshold AND no anti-pattern match
  Gate 3: tone guard passed (tone_guard_applied is True)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from franktheunicorn.review.antipattern import check_against_anti_patterns

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import PullRequest, ReviewDraft

logger = logging.getLogger(__name__)


def should_auto_post(
    draft: ReviewDraft,
    project_config: ProjectConfig,
) -> bool:
    """Check if a draft passes the triple gate for auto-posting.

    Gate 1: posting mode is confidence-gated (not draft-only).
    Gate 2: confidence >= threshold AND no anti-pattern match.
    Gate 3: tone guard was applied.
    """
    # Gate 1: Mode check.
    if project_config.posting.mode != "confidence-gated":
        return False

    # Gate 2: Confidence threshold + anti-pattern check.
    if draft.confidence < project_config.posting.confidence_threshold:
        logger.debug(
            "Auto-post gate 2 failed: confidence %.2f < threshold %.2f for draft %s",
            draft.confidence,
            project_config.posting.confidence_threshold,
            draft.pk,
        )
        return False

    matches = check_against_anti_patterns(draft.comment_body, draft.pull_request.project)
    if matches:
        logger.debug(
            "Auto-post gate 2 failed: anti-pattern match for draft %s",
            draft.pk,
        )
        return False

    # Gate 3: Tone guard check.
    if not draft.tone_guard_applied:
        logger.debug(
            "Auto-post gate 3 failed: tone guard not applied for draft %s",
            draft.pk,
        )
        return False

    return True


def auto_post_findings(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_config: OperatorConfig | None = None,
) -> list[ReviewDraft]:
    """Filter pending drafts through the triple gate and auto-post qualifying ones.

    Uses a separate bot token (GITHUB_TOKEN_BOT) for auto-posted reviews,
    distinct from the operator's draft account.

    Returns the list of drafts that were auto-posted.
    """
    from franktheunicorn.core.models import ReviewDraft as ReviewDraftModel

    # Gate 1: Early exit if not confidence-gated.
    if project_config.posting.mode != "confidence-gated":
        return []

    pending = list(
        ReviewDraftModel.objects.filter(
            pull_request=pr,
            status="pending",
        ).select_related("pull_request__project")
    )

    eligible: list[ReviewDraft] = []
    for draft in pending:
        if should_auto_post(draft, project_config):
            eligible.append(draft)

    if not eligible:
        return []

    # Resolve bot token.
    bot_token_env = project_config.posting.bot_token_env
    bot_token = os.environ.get(bot_token_env, "")
    if not bot_token:
        logger.warning(
            "Auto-posting enabled but %s not set. Skipping auto-post for PR #%d.",
            bot_token_env,
            pr.number,
        )
        return []

    # Post using the bot identity.
    try:
        from franktheunicorn.backends.github import GitHubClient
        from franktheunicorn.backends.poster import GitHubPoster

        client = GitHubClient(token=bot_token)
        try:
            attribution = "Auto-posted by franktheunicorn (confidence-gated)"
            poster = GitHubPoster(client, attribution=attribution)
            result = poster.post_review(pr, eligible)
            if result:
                logger.info(
                    "Auto-posted %d findings for PR #%d",
                    len(eligible),
                    pr.number,
                )
        finally:
            client.close()
    except Exception:
        logger.exception("Auto-posting failed for PR #%d", pr.number)
        return []

    return eligible
