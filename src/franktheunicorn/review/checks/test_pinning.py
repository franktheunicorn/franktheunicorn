"""Test-pinning sub-check — would the suite fail if the change were wrong?

The single largest bucket of agreed maintainer comments on Spark is not "add
a test" but "the test you added does not actually pin the change": it takes a
path that never reaches the new code, its assertion cannot distinguish the
intended behavior from the old one, or the only covering test is skipped in
the mode that changed. This check asks the sharper question the ``coverage``
check does not: given the test exists, does it fail when it should?

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
You are checking whether the tests in this pull request actually pin the \
change — whether the suite would fail if the new code were wrong, inverted, \
or reverted. A test that stays green when the change breaks is worse than no \
test: it says the change is covered when it is not.

Emit a finding when one of these is true:
- Reverting or inverting the change would keep every test green (the guard, \
the branch, or the reordering is never what decides an assertion)
- A new test never reaches the code it claims to cover: it takes an earlier \
exit, a different branch, or a mode where the change is inert
- An assertion cannot fail for the bug it describes: the input would pass on \
the old code too, it only checks presence or type where shape or value \
matters, or the expected values are identical either way (all null, all \
equal) so a swap or mis-wiring is invisible
- The test asserts a DERIVED value the change cannot affect — a count, a \
hash, a value computed in a worker or on the other side of a boundary — \
instead of the representation the change actually alters, so reverting the \
change leaves the assertion green
- The only test covering a path is skipped, or runs exclusively in a mode \
the change does not affect (the skipped/other-mode case is the coverage gap)

Name the specific test and what would have to happen for it to fail — the \
input, branch, or assertion that should carry the change but does not. Do \
NOT ask to add a test where none exists (another pass owns that ask); this \
check is only about tests the diff touches. Do NOT comment on test style, \
naming, or assertions that are fine. If the tests genuinely pin the change, \
return an empty findings array.

The finding body is one or two sentences. No patch.

{comment_voice}

{test_expectations}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "test-pinning" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class TestPinningCheck(BaseCheck):
    """Evaluates whether the PR's tests fail when the change breaks."""

    name = "test-pinning"

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
