"""Security sub-check — asks the LLM to evaluate security of PR changes."""

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
You are a security reviewer. Your ONLY job is to identify potential security \
vulnerabilities introduced or exposed by the changes in this pull request.

Focus on:
- Injection flaws (SQL injection, command injection, XSS, template injection)
- Authentication and authorization issues (missing auth checks, privilege escalation)
- Sensitive data exposure (hardcoded secrets, API keys, tokens, passwords in code)
- Insecure deserialization (pickle, yaml.load, eval, exec on untrusted input)
- Path traversal and file inclusion vulnerabilities
- Insecure cryptographic practices (weak algorithms, hardcoded IVs/salts)
- Server-side request forgery (SSRF)
- Race conditions with security implications
- Missing input validation or sanitization
- Insecure dependency usage patterns
- OWASP Top 10 categories generally

A finding is only real if you can name the trust boundary and why the input \
is attacker-reachable. Behavior the project security model (below, when \
present) declares trusted is not a finding. If you cannot name the boundary, \
return nothing for that hunk.

Write the body as a short maintainer comment: what looks wrong, the boundary, \
and a question if you are not sure it is reachable. No advisory, no patch, \
no category essay.

{comment_voice}

Do NOT comment on code style, naming, test coverage, architecture, or anything \
unrelated to security. If the changes introduce no security concerns, return an \
empty findings array.

{security_model_section}

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Prefix the title of every finding with "security:" (for example, "security: hardcoded API key").
If you have no findings, return: {{"findings": []}}
"""


class SecurityCheck(BaseCheck):
    """Evaluates whether a PR's changes introduce security vulnerabilities."""

    name = "security"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        system_prompt = _SYSTEM_PROMPT.format(
            schema=finding_schema_json(),
            security_model_section=format_security_model_section(pr_context.security_model),
            comment_voice=COMMENT_VOICE,
        )

        user_message = build_user_message(diff, pr_context)

        return system_prompt, user_message
