"""Coverage sub-check — asks the LLM to evaluate test coverage of a PR."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.checks import BaseCheck

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


@lru_cache(maxsize=1)
def _finding_schema() -> str:
    return json.dumps(ReviewFinding.model_json_schema(), indent=2)


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

_USER_TEMPLATE = """\
PR #{pr_number}: {pr_title}
Author: {pr_author}
Project: {project_name}

{pr_body_section}
Diff:
```diff
{diff}
```
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
            schema=_finding_schema(),
        )

        body_section = ""
        if pr_context.pr_body:
            body_preview = pr_context.pr_body[:2000]
            if len(pr_context.pr_body) > 2000:
                body_preview += "\n... (truncated)"
            body_section = f"PR description:\n{body_preview}\n"

        user_message = _USER_TEMPLATE.format(
            pr_number=pr_context.pr_number,
            pr_title=pr_context.pr_title,
            pr_author=pr_context.pr_author,
            project_name=pr_context.project_name,
            pr_body_section=body_section,
            diff=diff,
        )

        return system_prompt, user_message
