"""Security-context sub-check — evaluates whether applying a diff weakens
the security posture of the existing codebase."""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.review.prompt import (
    COMMENT_VOICE,
    build_user_message,
    finding_schema_json,
    format_security_model_section,
)

if TYPE_CHECKING:
    from franktheunicorn.review.backends.base import PRContext


_SYSTEM_PROMPT = """\
You are a security reviewer. Your ONLY job is to determine whether applying \
this diff to the existing codebase causes security issues in context.

Do NOT look for vulnerabilities introduced directly by new code — a separate \
check handles that. Instead, focus on how the changes interact with the \
surrounding code:

- Does removed or changed code weaken existing security controls (e.g., \
removing input validation, loosening auth checks, disabling CSRF protection)?
- Do the changes bypass an existing guard clause or security gate visible in \
the diff context?
- Do changes alter the security posture of callers or callees (e.g., a \
function that previously returned escaped HTML now returns raw HTML)?
- Do changes to configuration, middleware, or infrastructure files weaken \
the application's security boundary?
- Do the changes shift trust boundaries (e.g., moving code from an \
authenticated to an unauthenticated path, exposing internal APIs externally)?
- Could the interaction between changed and unchanged code create a race \
condition, TOCTOU, or privilege-escalation path?

A finding is only real if you can name the trust boundary that moved. \
Behavior the project security model (below, when present) declares trusted \
is not a finding. If you cannot name the boundary, return nothing for that hunk.

Write the body as a short maintainer comment: what changed, which boundary \
moved, and a question if you are not sure. No advisory, no patch.

{comment_voice}

Do NOT comment on code style, naming, test coverage, architecture, or anything \
unrelated to contextual security impact. If applying the changes does not \
weaken the security posture, return an empty findings array.

{security_model_section}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Prefix the title of every finding with "security-context:" (for example, \
"security-context: CSRF middleware removed").
If you have no findings, return: {{"findings": []}}
"""


class SecurityContextCheck(BaseCheck):
    """Evaluates whether applying a PR's changes weakens security in context."""

    name = "security-context"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        system_prompt = _SYSTEM_PROMPT.format(
            schema=finding_schema_json(),
            security_model_section=format_security_model_section(pr_context.security_model),
            comment_voice=COMMENT_VOICE,
        )

        user_message = build_user_message(diff, pr_context)

        return system_prompt, user_message
