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
        "Return your review as a JSON object with two keys:\n"
        '  - "overall_vibe": a short (1-3 sentence) plain-text impression of the PR\n'
        "    overall — strengths, concerns, and your gut feel as a reviewer.\n"
        '  - "findings": an array of finding objects matching this schema:\n'
        + finding_schema_json()
        + "\n\nFor the 'severity' field, prefer one of: 'critical', 'important', "
        "'nit', 'informational'. ('high'/'medium'/'low' will be mapped to "
        "'important'/'nit'/'nit' respectively, but using the canonical values "
        "is preferred.)\n"
        'If you have no line-specific findings, return "findings": [].'
        ' Always include "overall_vibe" with at least one sentence.\n'
        'Leave "suggestion" as "". That field is posted as a GitHub suggestion '
        "block and replaces the commented lines verbatim. Put the direction in "
        '"body" instead.'
    )


#: How the operator actually writes on a PR. Learned from Spark review
#: comments: short, informal, a question or a stated preference, not an
#: audit writeup. Shared with the agent-CLI prompt so the two paths cannot
#: drift. Do not tell the model to copy typos from the corpus.
COMMENT_VOICE = """\
Comment voice (the finding body is the GitHub comment, postable as-is):
- One or two sentences. A third only when the middle step is the point.
  Do not pad, do not restate the diff, do not open with a compliment.
- When the fact is settled, say it plainly ("Let's target 4.4, the 4.3 RC
  is about to cut so only bug fixes land there"). When it is a judgment,
  name the preference and the trade-off and invite pushback ("Maybe set
  this from the config instead of a param? Testing gets a bit more awkward,
  so I'm open to push back."). When you are not sure, say so and ask
  ("I'm not 100% sure the ordering is right — would building the sort once
  and applying it be simpler?").
- Questions are a normal comment, not a weak one: "Why do we need this?",
  "What's this for?", "Would it make sense to only start this when there's
  a Python job, or is that complexity not worth it?"
- "nit:" at the start for non-blocking structure. No other labels.
- Do not paste a replacement patch. The direction goes in the same prose.
  Quoting a line already in the diff, or a CI error, is fine when that
  quote is the evidence.
- Use "I" and "we". No character voice. Match the cadence, not any typos."""

#: What gets commented on, plus the voice above. Design and semantics first;
#: short nits on dead code and odd structure the PR added are in scope, and
#: "please add a test" is the comment when nothing covers the change. The
#: extra bullets are the overlap with other Spark committers' reviews that
#: she would actually leave: code that is not earning its keep, a test that
#: cannot fail, a leak on the failure path, and a doc that disagrees with
#: the code. Style nits stayed out on purpose.
REVIEWING_GUIDANCE = (
    """\
How to review (match the operator's actual review comments):
- Focus on design, semantics, and correctness — NULL/NaN handling, config
  vs. param, a default that contradicts the comment next to it, trust
  boundaries, ordering, API misuse, who releases a resource. These are the
  comments the operator actually leaves.
- Also flag dead or unused code this PR added, and structure that makes the
  change harder to follow ("nit: this is weird as a function, I'd rather
  inline the if"). Formatting, naming, and import order belong to the linter;
  do not surface those.
- Question code that is not earning its keep: an unreachable branch, an
  unused default, a helper called from one place, a collection whose order
  is never read, a Dataset or a collect built to answer something the plan
  already knows. "Why do we need this?" is a complete comment.
- Ask for a test when a behavior change has no coverage, or the existing
  tests do not actually cover it: they return early, the input would pass
  on the old code too, the assertion is vacuously true, or the suite would
  stay green if the new behavior were reverted. That is a normal comment.
  Do not ask when an existing test already exercises the path. Never
  critique test mechanics.
- Cleanup that runs only on the success path is a leak. The failure and
  cancellation paths close the same thing.
- If the comment, the doc, and the code disagree, that disagreement is the
  finding. Say which one is wrong.
- Do NOT paste a ready-made patch. Describe the concern and a direction in
  prose; the operator writes the fix.
- Out of scope but real: defer to a follow-up ticket, with a commitment to
  do it soon, and offer to file it. Do not block the PR on it.
- Question the target branch when the project cuts release branches. If you
  know the release state, state the target; if you do not, ask. See
  project-specific guidance when it is present.
- If another maintainer owns the surface, say to check with them instead of
  deciding it.
- Skip a finding you would not leave.

"""
    + COMMENT_VOICE
)


def format_security_model_section(security_model: str) -> str:
    """Render the project's trust-boundary text for a security review prompt.

    Empty when the project has not documented one, so the prompt does not
    grow a header that says nothing. Behavior the text calls trusted is not
    a finding; the caller says so around this block.
    """
    text = (security_model or "").strip()
    if not text:
        return ""
    return (
        "Project security model / trust boundaries (authoritative — behavior "
        "this declares trusted is NOT a finding):\n" + text + "\n"
    )


def build_system_prompt(ctx: PRContext) -> str:
    """Build the system prompt from project and operator context."""
    if ctx.personality_identity:
        parts = [
            ctx.personality_identity,
            "",
            "Operator-facing summaries (overall_vibe, digest) use this voice:",
            ctx.personality_internal_voice,
        ]
        if ctx.personality_external_voice:
            parts.extend(
                [
                    "",
                    "Finding bodies are GitHub review comments. Write those in this",
                    "voice, not the one above:",
                    ctx.personality_external_voice,
                ]
            )
        parts.extend(
            [
                "",
                f"Review style: {ctx.review_style}.",
                f"Tone: {ctx.tone}.",
            ]
        )
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
    parts.append(REVIEWING_GUIDANCE)
    if ctx.review_guidance and ctx.review_guidance.strip():
        parts.append("")
        parts.append("Project-specific review guidance (treat as authoritative):")
        parts.append(ctx.review_guidance.strip())
    if ctx.review_areas_of_interest:
        parts.append("")
        parts.append("Areas of interest — flag for extra consideration when the PR touches these:")
        for area in ctx.review_areas_of_interest:
            if area.strip():
                parts.append(f"  - {area.strip()}")
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

    # Local-source context (read from the checkout, before the diff so the
    # reviewer can ground hunks in surrounding code).
    if ctx.full_file_context:
        header_parts.append(f"\n{ctx.full_file_context}")
    if ctx.imported_modules_context:
        header_parts.append(f"\n{ctx.imported_modules_context}")

    header_parts.append(f"\nDiff:\n```diff\n{diff}\n```")

    # Repo health context (from git history analysis — trusted, locally generated).
    if ctx.repo_health_context:
        header_parts.append(f"\n[Repo health analysis]\n{ctx.repo_health_context}")

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


__all__ = [
    "COMMENT_VOICE",
    "REVIEWING_GUIDANCE",
    "build_system_prompt",
    "build_user_message",
    "finding_schema_json",
    "format_security_model_section",
]
