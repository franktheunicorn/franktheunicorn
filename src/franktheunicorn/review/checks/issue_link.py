"""Issue-link sub-check — asks the LLM whether a PR addresses its linked issue(s)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are an issue-link reviewer. Your ONLY job is to evaluate whether this \
pull request actually addresses the issue(s) it references.

You will be given:
- The PR title, description, and diff
- The content of the linked issue(s) (GitHub issues and/or JIRA tickets)

Evaluate:
- Does the diff address what the linked issue describes?
- Are there signs the issue reference is wrong (e.g. the issue is about \
feature X but the PR changes feature Y)?
- Is the PR body referencing an issue for tracking purposes but the code \
changes are clearly unrelated?
- For JIRA tickets: does the ticket summary/description match the PR's \
actual changes?

Do NOT comment on code quality, style, test coverage, or anything unrelated \
to whether the PR matches its linked issue(s). If the link looks correct, \
or if there are no linked issues to validate, return an empty findings array.

{issue_context}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to "important" if the PR clearly does not address the linked \
issue, or "nit" if the connection is weak or ambiguous.
Set category to "issue-link" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class IssueLinkCheck(BaseCheck):
    """Evaluates whether a PR actually addresses the issue(s) it references."""

    name = "issue-link"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        issue_parts: list[str] = []
        if pr_context.linked_issues_context:
            issue_parts.append("Linked GitHub issue(s):\n" + pr_context.linked_issues_context)
        if pr_context.jira_context:
            issue_parts.append("Linked JIRA ticket:\n" + pr_context.jira_context)

        if not issue_parts:
            issue_context = (
                "No linked issues were found in this PR. Return an empty findings array."
            )
        else:
            issue_context = "\n\n".join(issue_parts)

        system_prompt = _SYSTEM_PROMPT.format(
            issue_context=issue_context,
            schema=finding_schema_json(),
        )

        user_message = build_user_message(diff, pr_context)

        return system_prompt, user_message

    def parse_response(self, raw_text: str) -> list[ReviewFinding]:
        """Parse LLM output, returning empty list when no issues are linked."""
        return super().parse_response(raw_text)
