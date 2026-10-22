"""Error-framework sub-check — new error paths and the error registry agree.

Projects with a structured error framework (registered error conditions,
error-classes JSON, typed exceptions) require errors to go through it. The
agreed-comment patterns: removing the last raise site of a condition but
leaving its registry entry dead; a user-fixable mistake mapped to an
internal-error class; an error message that contradicts the documented
meaning of its condition; a new error thrown as a raw exception with an
inline message where the project registers conditions.

See docs/review-categories.md (HyukjinKwon corpus audit) for the source
patterns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import COMMENT_VOICE, build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are checking whether this pull request's error paths stay consistent \
with the project's structured error framework: registered error conditions, \
the error-classes/conditions registry, and typed exceptions. The framework \
only works if every error goes through it and the registry says what each \
condition means.

Emit a finding when one of these is true:
- The diff removes the last place an error condition is raised but leaves \
its registry entry (a dead entry future readers will chase)
- A new error path uses a raw exception with an inline message where the \
project registers conditions and throws typed errors
- A user-fixable mistake (bad input, wrong ID, unsupported option) maps to \
an internal/unknown-server-error class, sending the user looking for a bug \
that is theirs to fix
- An error's message or docs contradict the registered meaning of its \
condition (the condition says one audience, the message says another)
- A test asserts an error by matching message text where the project's \
convention is to assert the condition or error class

Name the condition, class, or registry file involved. Do NOT propose \
renaming conditions this PR does not touch, and do NOT comment on error \
message style. If the diff's error handling is consistent (or adds no error \
paths), return an empty findings array.

The finding body is one or two sentences. No patch.

{comment_voice}

{review_guidance}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "error-framework" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class ErrorFrameworkCheck(BaseCheck):
    """Evaluates error paths against the project's error framework."""

    name = "error-framework"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        guidance = ""
        if pr_context.review_guidance:
            guidance = f"Project error-framework conventions: {pr_context.review_guidance}"
        system_prompt = _SYSTEM_PROMPT.format(
            review_guidance=guidance,
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message
