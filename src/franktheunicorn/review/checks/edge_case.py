"""Edge-case sub-check — null, NaN, overflow, boundaries, special values."""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import COMMENT_VOICE, build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are checking this pull request for unhandled edge cases: null, NaN, \
empty or single-element collections, numeric overflow, min/max boundaries, \
and special values (epoch, 'today', 'now', year 9999).

Emit a finding when one of these is true:
- A value that can be null, NaN, or empty reaches code that assumes it is not
- An arithmetic expression can overflow its type before it is widened or checked
- A boundary — first, last, zero-length, maximum precision — is reachable and \
handled incorrectly or not at all
- A special or sentinel value is folded, cast, or compared in a way that \
changes its meaning

Name the specific input and what goes wrong with it. Do NOT list edge cases \
that the code or an existing guard already handles, and do NOT speculate about \
inputs that cannot reach the changed code. Do NOT comment on style, tests, or \
documentation. If the edges look handled, return an empty findings array.

The finding body is one or two sentences naming the input and the failure. \
No patch.

{comment_voice}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "edge-case" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class EdgeCaseCheck(BaseCheck):
    """Evaluates null/NaN/overflow/boundary handling."""

    name = "edge-case"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        system_prompt = _SYSTEM_PROMPT.format(
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message
