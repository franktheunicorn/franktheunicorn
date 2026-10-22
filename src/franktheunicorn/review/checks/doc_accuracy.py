"""Doc-accuracy sub-check — do comments/scaladoc match the code?

Distinct from pr-description (which audits the PR body): this is in-code
comment and scaladoc/javadoc drift, one of the largest human review buckets
on Spark (see docs/review-categories.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import COMMENT_VOICE, build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are checking whether the comments and scaladoc/javadoc this pull request \
adds — or leaves next to code it changed — actually match what the code does. \
Reviewers flag this constantly: a doc that promises a guarantee the code does \
not enforce, a comment describing the old behavior, a stale line reference.

Emit a finding when one of these is true:
- A comment or doc states a behavior or guarantee the adjacent code does not \
provide
- A comment describes code that this change has since altered, so the comment \
is now wrong (a stale comment)
- A comment references a symbol, line, or method that the change removed or \
renamed — including a reference that names something that does not exist in \
the tree at all (a class, suite, or config a reader following it cannot find)

Quote the mismatch: what the text says versus what the code does. Do NOT ask \
for more comments where none are missing, and do NOT comment on wording, \
grammar, or style — only on comments that are factually wrong about the code. \
If the docs match, return an empty findings array.

The finding body is one or two sentences quoting the claim and naming the \
behavior that contradicts it. No patch.

{comment_voice}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "doc-accuracy" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class DocAccuracyCheck(BaseCheck):
    """Evaluates whether comments/scaladoc match the code."""

    name = "doc-accuracy"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        system_prompt = _SYSTEM_PROMPT.format(
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message
