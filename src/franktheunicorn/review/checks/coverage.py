"""Coverage sub-check — asks the LLM to evaluate test coverage of a PR."""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import COMMENT_VOICE, build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are checking whether a behavior change in this pull request is actually \
verified. Asking for a test is the right comment when nothing covers the \
change, or the existing tests do not really cover it.

Emit a finding when one of these is true:
- A behavior change has no test that reaches the new path
- A test is present but misses the change: it returns early, the input would \
pass on the old code too, the assertion is vacuously true, or the suite \
would stay green if the new behavior were reverted
- An existing test that covered the old behavior is now wrong

Do NOT ask to add a test for a change that an existing test already \
exercises. Do NOT comment on assertion style, message text, fixture layout, \
or naming. Do NOT comment on style, architecture, or security. If the \
changed behavior looks covered, return an empty findings array.

The finding body is one or two sentences naming the behavior that is \
unverified and why the existing tests miss it. Ask for a test. No patch.

{comment_voice}

{test_expectations}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "test-coverage" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class CoverageCheck(BaseCheck):
    """Evaluates whether a PR's changes have adequate test coverage."""

    name = "coverage"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        test_exp = ""
        if pr_context.test_expectations:
            test_exp = f"Project test expectations: {pr_context.test_expectations}"

        system_prompt = _SYSTEM_PROMPT.format(
            test_expectations=test_exp,
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )

        user_message = build_user_message(diff, pr_context)

        return system_prompt, user_message
