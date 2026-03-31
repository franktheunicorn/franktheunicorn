"""Shared prompt construction for all LLM review backends."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


@lru_cache(maxsize=1)
def finding_schema_json() -> str:
    """Return the ReviewFinding JSON schema as a formatted string.

    Public helper reused by sub-checks that need the schema in custom prompts.
    """
    from franktheunicorn.review.backends.base import ReviewFinding

    return json.dumps(ReviewFinding.model_json_schema(), indent=2)


@lru_cache(maxsize=1)
def _finding_schema() -> str:
    """Generate the full schema instruction block for the default review prompt."""
    return (
        "Return your review as a JSON array of finding objects matching this schema:\n"
        + finding_schema_json()
        + "\n\nIf you have no findings, return an empty array: []"
        '\nWrap the array in {"findings": [...]}.'
    )


def build_system_prompt(ctx: PRContext) -> str:
    """Build the system prompt from project and operator context."""
    if ctx.personality_identity:
        parts = [
            ctx.personality_identity,
            "",
            ctx.personality_internal_voice,
            "",
            f"Review style: {ctx.review_style}.",
            f"Tone: {ctx.tone}.",
        ]
    else:
        parts = [
            "You are a code reviewer acting on behalf of an open-source maintainer.",
            f"Review style: {ctx.review_style}.",
            f"Tone: {ctx.tone}.",
        ]

    if ctx.review_context and ctx.review_context != "general open-source":
        parts.append(f"Project context: {ctx.review_context}")

    if ctx.governance and ctx.governance != "standard":
        parts.append(f"Governance model: {ctx.governance}.")

    if ctx.test_expectations:
        parts.append(f"Test expectations: {ctx.test_expectations}.")

    if ctx.anti_patterns:
        parts.append(
            "IMPORTANT: Do NOT produce comments matching these anti-patterns "
            "(the operator has rejected similar comments before):"
        )
        for ap in ctx.anti_patterns:
            parts.append(f"  - {ap}")

    if ctx.personality_review_philosophy:
        parts.append("")
        parts.append(ctx.personality_review_philosophy)

    parts.append("")
    parts.append(_finding_schema())

    return "\n".join(parts)


def build_user_message(diff: str, ctx: PRContext) -> str:
    """Build the user message containing PR metadata and the diff."""
    header_parts = [
        f"PR #{ctx.pr_number}: {ctx.pr_title}",
        f"Author: {ctx.pr_author}",
        f"Project: {ctx.project_name}",
    ]

    if ctx.pr_body:
        # Truncate very long PR bodies to keep prompt reasonable.
        body_preview = ctx.pr_body[:2000]
        if len(ctx.pr_body) > 2000:
            body_preview += "\n... (truncated)"
        header_parts.append(f"\nPR description:\n{body_preview}")

    header_parts.append(f"\nDiff:\n```diff\n{diff}\n```")

    # v1.5: inject external context (labeled as untrusted).
    from franktheunicorn.data_access.context_orchestrator import format_context_for_prompt

    external_ctx = format_context_for_prompt(
        community_ctx=ctx.community_context,
        jira_ctx=ctx.jira_context,
        sentry_ctx=ctx.sentry_context,
    )
    if external_ctx:
        header_parts.append(external_ctx)

    return "\n".join(header_parts)


__all__ = ["build_system_prompt", "build_user_message", "finding_schema_json"]
