"""Design sub-check — a simpler/safer alternative, or a cross-cutting impact.

The hardest and most expensive comment to get wrong, so it is deliberately
conservative and uses the project's review_guidance / areas of interest to
stay grounded in the subsystems that actually matter (see
docs/review-categories.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import COMMENT_VOICE, build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are considering whether this pull request's approach has a problem its \
author may not have weighed: a concretely simpler or safer way to get the \
same result, or an interaction with another part of the system that the diff \
does not account for.

Emit a finding ONLY when one of these is true:
- There is a specific, simpler or safer alternative that achieves the same \
result, and you can name it
- The change interacts with a specific named subsystem (for example AQE, the \
catalog, checkpointing, the shuffle) in a way the PR does not address

This is the rarest and most expensive comment to get wrong. If you are not \
confident and specific, emit nothing — an empty findings array is the right \
answer for most PRs. Do NOT restate the change, propose speculative \
refactors, or raise style, tests, or documentation. Pose it the way a senior \
reviewer would, as a question that names the alternative or the subsystem.

The finding body is one or two sentences. No patch.

{review_guidance}{areas_of_interest}{comment_voice}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "design" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class DesignCheck(BaseCheck):
    """Evaluates approach: simpler alternative or cross-cutting impact."""

    name = "design"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        guidance = ""
        if pr_context.review_guidance:
            guidance = f"Project review guidance: {pr_context.review_guidance}\n\n"
        areas = ""
        if pr_context.review_areas_of_interest:
            joined = "; ".join(pr_context.review_areas_of_interest)
            areas = f"Areas to weigh when touched: {joined}\n\n"
        system_prompt = _SYSTEM_PROMPT.format(
            review_guidance=guidance,
            areas_of_interest=areas,
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message
