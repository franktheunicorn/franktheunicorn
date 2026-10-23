"""Correctness sub-check — does the change do what it claims, correctly?

The highest-value gap versus how humans review Spark (see
docs/review-categories.md): the main review prompt covers it, but a focused
pass catches logic bugs the generalist misses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import COMMENT_VOICE, build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are checking whether this pull request's change is logically correct — \
whether it does what the PR says and preserves the invariants of the code it \
touches. This is the comment a maintainer makes when the code will produce a \
wrong result, miss a case, or break an assumption the rest of the system \
relies on.

Emit a finding when one of these is true:
- The change yields an incorrect result for some reachable input
- It breaks an invariant or contract the surrounding code depends on
- A condition is inverted, off by one, or matches only the first of several \
cases when it should match all
- It silently changes behavior the PR did not intend to change
- A path meant to degrade gracefully does not: it warns, logs, or flags the \
problem and then still throws or aborts, when the stated intent (a comment, \
the PR description, a sibling case) is to warn and keep going

Ground every finding in the diff: name the input or state that triggers it and \
the wrong outcome. If you cannot name a concrete case, do not emit it — a \
speculative "this might be unsafe" is noise a reviewer would not post. Do NOT \
comment on style, tests, documentation, or performance; other passes own \
those. If the change looks correct, return an empty findings array.

The finding body is one or two sentences naming the triggering case and the \
wrong result. No patch.

{comment_voice}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "correctness" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class CorrectnessCheck(BaseCheck):
    """Evaluates whether a PR's change is logically correct."""

    name = "correctness"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        system_prompt = _SYSTEM_PROMPT.format(
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message
