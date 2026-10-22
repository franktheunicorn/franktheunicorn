"""Config-docs sub-check — a config and its documentation must agree.

A config knob is API: its since-version, its docs-table row, and whether it
is user-facing or internal are all things reviewers actually catch. The
agreed-comment patterns: a user-facing conf missing from the configuration
docs table; an ``.internal()`` conf that user docs tell users to set; a
since-version that names the wrong branch's release; a behavior-changing
default with no migration-guide entry.

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
You are checking whether the configuration knobs this pull request adds or \
changes are consistent with how the project documents and versions them. A \
config is API: users find it through the configuration docs, learn its \
version from the since-version, and learn its scope from whether it is \
internal.

Emit a finding when one of these is true:
- A new user-facing config is missing from the project's configuration docs \
(where the project keeps a table or page listing user-visible knobs)
- A config is marked internal/hidden but user-facing docs or error messages \
tell users to set it — or the reverse, an internal knob surfaced as if it \
were supported
- The since-version does not follow the project's versioning policy for the \
branch the PR targets (a patch release that documents a feature as added in \
the next major, or a since-version naming a different branch's release)
- A default flip or user-visible behavior change has no migration-guide \
entry where the project requires one
- A config whose default is derived from another config does not say where \
the effective value comes from

Cite the config key and the doc or policy it disagrees with. Do NOT comment \
on the config's design, naming, or whether it should exist. If the diff adds \
no config or the docs are consistent, return an empty findings array.

The finding body is one or two sentences. No patch.

{comment_voice}

{review_guidance}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Set category to "config-docs" in the title field of every finding.
If you have no findings, return: {{"findings": []}}
"""


class ConfigDocsCheck(BaseCheck):
    """Evaluates config ↔ docs/versioning consistency."""

    name = "config-docs"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        guidance = ""
        if pr_context.review_guidance:
            guidance = f"Project config/versioning conventions: {pr_context.review_guidance}"
        system_prompt = _SYSTEM_PROMPT.format(
            review_guidance=guidance,
            schema=finding_schema_json(),
            comment_voice=COMMENT_VOICE,
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message
