"""Concurrency / resource / memory sub-check.

The second-largest gap on a data engine like Spark (see
docs/review-categories.md): memory held across batches, leaks, spill
accounting, and thread-safety — correct-looking code that fails under load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import COMMENT_VOICE, build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are checking this pull request for concurrency and resource-management \
problems: memory held longer than intended, leaks, spill or memory-accounting \
mistakes, and thread-safety. On a data-processing engine these are where \
correct-looking code fails under load.

Emit a finding when one of these is true:
- Memory, a buffer, or a resource is held across a boundary it was freed \
before (a batch, a row group, a task), or is freed twice, or not at all
- Memory accounting reports a different amount to the manager than is \
actually held or released
- A field read or written from more than one thread has no synchronization, \
or a lock is acquired on a path that can return or throw before releasing it
- A listener or callback can run before the thing it cleans up has stopped
- A wait with no time bound, no interrupt handling, or no log line at the \
point it can stall — a hang here is invisible until somebody notices the job \
never finished

Name the sequence of events that triggers it — which thread, which batch, \
which error path. Do NOT emit a generic "this could have a race"; if you \
cannot trace the interleaving or the lifetime, skip it. Do NOT comment on \
style, tests, or documentation. If nothing here is wrong, return an empty \
findings array.

The finding body is one or two sentences naming the sequence and the leak, \
double-free, or unsafe access it causes. No patch.

{comment_voice}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "concurrency" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class ConcurrencyCheck(BaseCheck):
    """Evaluates concurrency, memory, and resource-lifetime problems."""

    name = "concurrency"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        system_prompt = _SYSTEM_PROMPT.format(
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message
