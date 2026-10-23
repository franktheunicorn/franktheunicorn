"""Simplification sub-check — code this change made redundant, or a rule it
had to edit twice.

Category F of the original audit (docs/review-categories.md), drafted then
but never implemented. The Dongjoon-Hyun agreed-corpus (2026-10-22) makes it
the largest single unautomated bucket: his most-repeated agreed comment is
not a style nit but "this is now redundant" — a condition the change made
always-true, a wrapper that only forwards, a hand-rolled copy of a utility
the project already has, state duplicated with a sibling, or a rule the PR
had to edit in two places because it exists in two copies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import COMMENT_VOICE, build_user_message, finding_schema_json

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are checking whether this pull request leaves code that is not earning \
its keep: something the change made redundant, something that duplicates a \
utility the project already has, or a rule that exists in copies this PR had \
to edit more than once. This is the maintainer's "nit:" on structure — but \
the point is always a specific redundancy, never taste.

Emit a finding when one of these is true:
- The change makes existing or added code a no-op: a condition that is now \
always true or always false, a branch that can no longer be reached, a \
parameter or return value that is always the same thing, a flag that nothing \
reads
- The PR had to edit the SAME rule in two or more places (a condition, a \
comment, a constant copied across files) — that lockstep edit is the \
finding: the copies will drift on the next change, and a shared helper or \
single definition removes the sync burden
- Added code re-implements a utility that already exists — in the project's \
own utils or test base, the standard library, or a common dependency the \
project already carries — and the existing one is a drop-in
- A wrapper, forwarder, or re-export that only passes its arguments through, \
with no added behavior, keeping two names for one thing
- State that duplicates state a sibling or the same class already holds, so \
the two can disagree
- A value is recomputed on every use (a method call in a loop, an \
allocation per element) when the project's own pattern is to hold it in a \
local, and the recomputation is observable (a connector may build a new \
array per call, a copy per byte)
- Test scaffolding the PR added that duplicates a fixture or helper the \
suite already has

Name the redundancy: what is now dead, which utility to use, or which two \
copies had to move together. Do NOT restate the duplication detector's \
output (verbatim blocks copied between files are its job); do NOT propose \
extracting a shared helper for code that is merely similar; do NOT comment \
on style, formatting, or naming taste. If everything earns its keep, return \
an empty findings array.

The finding body is one or two sentences. No patch.

{comment_voice}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "simplification" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class SimplificationCheck(BaseCheck):
    """Evaluates whether the change leaves redundant or duplicated-need code."""

    name = "simplification"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        system_prompt = _SYSTEM_PROMPT.format(
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message
