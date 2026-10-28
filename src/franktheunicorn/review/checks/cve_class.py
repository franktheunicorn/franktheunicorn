"""CVE-class sub-check — does this PR introduce a known class of CVE?

Injects the project's curated CVE-class catalog (grouped from published
advisories) into the prompt and asks the agent to find anything in the diff
that matches one of those classes. Findings are prefixed ``cve-class:`` so
they're easy to triage alongside the generic ``security:`` findings.

When the project has no catalog the prompt says so and the model returns
empty; the check logs a WARNING so the misconfiguration is visible (the
operator adds ``cve_class_catalog`` to the project config or drops
``cve-class`` from ``llm_checks``).
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are a security reviewer. Your ONLY job is to find whether this PR \
introduces a vulnerability that matches a class of CVE this project has \
shipped before.

The catalog below groups the CVE classes this project has had. For each \
class, the "Look for" line names the shape of code that introduced a real \
CVE. A finding is only real if you can name the trust boundary AND why the \
input is attacker-reachable, and the change matches one of these classes. \
Behavior the project security model (below, when present) declares trusted \
is not a finding.

{catalog_section}

{security_model_section}

For each match, write a short maintainer comment: the class, the boundary, \
and where in the diff it appears. No advisory, no patch.

{comment_voice}

If the changes introduce nothing matching these classes, return an empty \
findings array.

Return your review as a JSON object: {{"findings": [...]}}
Each finding must match this schema:
{schema}

Set severity to one of: critical, important, nit, informational.
Prefix the title of every finding with "cve-class:" (for example, \
"cve-class: shell command injection from UI input").
If you have no findings, return: {{"findings": []}}"""


class CVEClassCheck(BaseCheck):
    """Flags PR changes that match a class of CVE the project has shipped."""

    name = "cve-class"

    def __init__(self, catalog: str = "") -> None:
        if not (catalog or "").strip():
            logger.warning(
                "cve-class check instantiated with no CVE-class catalog; the "
                "check will run but find nothing. Add cve_class_catalog to the "
                "project config or remove cve-class from llm_checks."
            )
        self.catalog = catalog

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        system_prompt = _SYSTEM_PROMPT.format(
            schema=finding_schema_json(),
            security_model_section=format_security_model_section(pr_context.security_model),
            comment_voice=COMMENT_VOICE,
            catalog_section=self._catalog_section(),
        )
        user_message = build_user_message(diff, pr_context)
        return system_prompt, user_message

    def _catalog_section(self) -> str:
        text = (self.catalog or "").strip()
        if not text:
            return "No CVE-class catalog is configured for this project."
        return "CVE classes this project has shipped (from published advisories):\n" + text + "\n"
