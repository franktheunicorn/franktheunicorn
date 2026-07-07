"""Shepherding mode — draft responses to reviewer comments on operator's PRs (v2 — §2.3).

Detects new reviewer comments, generates draft responses using the LLM,
and surfaces rebase-needed / staleness alerts as informational findings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

from franktheunicorn.core.models import ReviewDraft

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)

# Heuristic for detecting questions in reviewer comments.
_QUESTION_RE = re.compile(r"\?\s*$", re.MULTILINE)
_QUESTION_WORDS = re.compile(
    r"(?i)\b(why|how|what|when|where|could you|can you|would you|"
    r"is there|are there|do you|did you|have you|should we)\b"
)

# Staleness threshold — PR with no activity for this many days.
STALENESS_DAYS = 14


@dataclass
class ReviewerComment:
    """A comment from a reviewer on an operator's PR."""

    author: str
    body: str
    created_at: datetime
    file_path: str = ""
    line_number: int | None = None
    is_question: bool = False


def detect_questions(body: str) -> bool:
    """Heuristic: does this comment contain a question?"""
    return bool(_QUESTION_RE.search(body) or _QUESTION_WORDS.search(body))


def _build_shepherd_prompt(
    comment: ReviewerComment,
    pr: PullRequest,
    project_config: ProjectConfig,
) -> str:
    """Build the LLM prompt for drafting a response to a reviewer comment."""
    parts = [
        f"You are the author of PR #{pr.number} in {pr.project.full_name}.",
        f"PR title: {pr.title}",
    ]
    if pr.body:
        parts.append(f"PR description: {pr.body[:500]}")
    parts.append(f"\nReviewer @{comment.author} commented:")
    if comment.file_path:
        parts.append(f"  On file: {comment.file_path}")
        if comment.line_number:
            parts.append(f"  At line: {comment.line_number}")
    parts.append(f'  "{comment.body}"')
    parts.append(
        "\nDraft a concise, helpful response as the PR author. "
        "Acknowledge the feedback and either explain your reasoning or commit to a fix."
    )
    return "\n".join(parts)


def generate_shepherd_drafts(
    pr: PullRequest,
    reviewer_comments: list[ReviewerComment],
    operator_config: OperatorConfig,
    project_config: ProjectConfig,
) -> list[ReviewDraft]:
    """Generate draft responses to reviewer comments on operator's own PRs.

    Returns ReviewDraft objects with sources=["shepherding"].
    """
    from franktheunicorn.review.backends import get_backend

    if not reviewer_comments:
        # Still check for condition alerts even with no new comments.
        return _generate_condition_alerts(pr)

    # Get the first available backend for response generation.
    backend_configs = operator_config.llm_backends
    if not backend_configs:
        from franktheunicorn.config.models import LLMBackendConfig

        backend_configs = [LLMBackendConfig()]

    backend_config = backend_configs[0]
    backend = get_backend(backend_config)

    drafts: list[ReviewDraft] = []

    for comment in reviewer_comments:
        prompt = _build_shepherd_prompt(comment, pr, project_config)

        try:
            from franktheunicorn.review.backends.base import PRContext

            # Build a minimal PRContext for the backend's generate_findings call.
            shepherd_context = PRContext(
                pr_title=pr.title,
                pr_body=prompt,
                pr_author=pr.author,
                pr_number=pr.number,
                project_name=pr.project.full_name,
                review_context=project_config.review_context,
                review_style="concise and helpful",
                tone=project_config.tone,
                test_expectations="",
                governance=project_config.governance,
            )

            findings = backend.generate_findings("", shepherd_context)
            response_text = findings[0].body if findings else "Thank you for the feedback."

        except Exception:
            logger.debug("LLM call failed for shepherding, using placeholder.")
            response_text = "Thank you for the feedback. I'll look into this."

        if not response_text:
            response_text = "Thank you for the feedback."

        draft = ReviewDraft.objects.create(
            pull_request=pr,
            file_path=comment.file_path,
            line_number=comment.line_number,
            comment_body=response_text,
            confidence=0.6,
            sources=["shepherding"],
            category="other",
            severity="informational",
            reasoning_trace=f"Response to @{comment.author}: {comment.body[:100]}",
            backend_used=backend_config.provider,
            status="pending",
        )
        drafts.append(draft)

    # Check for condition alerts.
    condition_drafts = _generate_condition_alerts(pr)
    drafts.extend(condition_drafts)

    return drafts


def _generate_condition_alerts(pr: PullRequest) -> list[ReviewDraft]:
    """Generate informational alerts for PR conditions (rebase, staleness).

    Alerts are keyed on a *stable* ``backend_used`` marker: keying on text
    that embeds the day count would mint a fresh duplicate draft every day.
    Day counts belong only in the (default-only) comment body.
    """
    alerts: list[ReviewDraft] = []

    # Rebase needed.
    if pr.mergeable is False:
        alert, _created = ReviewDraft.objects.get_or_create(
            pull_request=pr,
            backend_used="shepherd-rebase",
            defaults={
                "comment_body": "This PR has merge conflicts and needs a rebase.",
                "confidence": 1.0,
                "category": "other",
                "severity": "informational",
                "status": "pending",
                "sources": ["shepherding"],
                "reasoning_trace": "Detected mergeable=False from GitHub API",
            },
        )
        alerts.append(alert)

    # Staleness check.
    if pr.github_updated_at:
        age = timezone.now() - pr.github_updated_at
        if age > timedelta(days=STALENESS_DAYS):
            alert, _created = ReviewDraft.objects.get_or_create(
                pull_request=pr,
                backend_used="shepherd-stale",
                defaults={
                    "comment_body": (
                        f"This PR has had no activity for {age.days} days. "
                        "Consider pinging reviewers or closing if no longer needed."
                    ),
                    "confidence": 1.0,
                    "category": "other",
                    "severity": "informational",
                    "status": "pending",
                    "sources": ["shepherding"],
                    "reasoning_trace": f"PR inactive for {age.days} days",
                },
            )
            alerts.append(alert)

    return alerts
