"""Parity sub-check — dual surfaces that must move together.

Projects with parallel implementations of one surface (a classic client and
a connect/server client, an Arrow path and a non-Arrow path, a JVM
implementation and its Python mirror) require a change to land in both or be
deliberately justified. The recurring agreed-comment pattern: one surface is
fixed, the sibling keeps the old behavior, and the only test that would
catch the divergence is skipped in the other mode.

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
You are checking whether this pull request keeps parallel implementations of \
the same surface in step. Many projects carry dual paths for one feature: a \
classic client and a connect/server client, an Arrow path and a non-Arrow \
path, a JVM implementation and its Python mirror, an interpreted path and a \
codegen path. A change to one copy is half a fix when the paths are supposed \
to agree; when they are allowed to differ, the difference should be stated, \
not discovered later by a user.

Emit a finding when one of these is true:
- The diff changes one of two parallel paths without the sibling, and the \
project's own tests, docs, or comments treat the two as equivalent
- The diff changes one copy of a conversion, comparison, or constant that has \
a sibling elsewhere — another converter for the same type in the same file, a \
classic client's mirror of the changed connect client, a JVM implementation \
of the changed Python helper — and the sibling is not changed and the \
difference is not stated. The sibling is usually OUTSIDE the hunks: name the \
file and the symbol so the author can go straight there
- A test that would catch divergence between the surfaces is skipped, or \
exists in only one mode, so the divergence is invisible to CI
- The change handles a case differently from how a sibling handles the \
analogous case (the NaN check, the null fallback, the warning) when the two \
are supposed to agree — name the sibling and its treatment
- The same term or rule is defined one way here and differently where it is \
also used (a type name, a mode, a "widest vs tightest" rule), and the change \
follows only one definition
- Parallel bookkeeping files that must change together with a build or \
packaging change were not all updated: the module/dependency definition and \
the build system's module list, a library's classic and connect packaging \
lists, a workflow's declared secrets and the names the job passes
- A parity claim the PR makes (in docs, comments, or test assertions) holds \
only under a runtime, JDK, or library version the project does not require
- Two copies of the same logic (a type matrix, a conversion, a constant) now \
disagree because only one was edited

Name the two surfaces and where the sibling lives (a file, a class, or a \
skipped test), so the author can go straight there. Do NOT flag parallel \
paths that already disagree for documented reasons this PR does not touch. \
Do NOT comment on style, tests in general, or documentation wording. If the \
surfaces are in step, return an empty findings array.

The finding body is one or two sentences. No patch.

{comment_voice}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "parity" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class ParityCheck(BaseCheck):
    """Evaluates whether dual-surface changes land in both copies."""

    name = "parity"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        system_prompt = _SYSTEM_PROMPT.format(
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message
