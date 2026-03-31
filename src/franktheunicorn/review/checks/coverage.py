"""Coverage sub-check — asks the LLM to evaluate test coverage of a PR."""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are a test-coverage reviewer. Your ONLY job is to evaluate whether the \
changes in this pull request have adequate test coverage.

Focus on:
- New code paths that lack corresponding tests
- Edge cases in new logic that are not exercised by tests
- Modified behaviour whose existing tests may no longer be sufficient
- Test files that were expected but are missing from the diff

Do NOT comment on code style, naming, architecture, or anything unrelated \
to test coverage. If test coverage looks adequate, return an empty findings array.

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
        )

        user_message = build_user_message(diff, pr_context)

        return system_prompt, user_message
