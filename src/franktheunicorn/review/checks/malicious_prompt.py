"""Malicious-prompt sub-check.

Runs the security pre-filter on the PR diff + description. Unlike other
checks, hits do not produce ReviewDrafts — they are filed as
SecurityReport rows so the operator triages them in the security tab.
The check still returns an informational ReviewFinding so the dashboard
shows that the scan ran and found something.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.checks import BaseCheck

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.review.backends.base import PRContext

logger = logging.getLogger(__name__)


class MaliciousPromptCheck(BaseCheck):
    """Pre-filter check for prompt-injection attempts in PR text.

    Stores a reference to the originating ``PullRequest`` so it can file a
    ``SecurityReport`` when the detector flags the content. The standard
    check pipeline does not pass the PR object into ``build_prompt``, so
    we override ``run`` (called from ``run_enabled_checks``) instead.
    """

    name = "malicious-prompt"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        # Unused — this check bypasses the standard prompt path. Implemented
        # to satisfy the abstract base.
        return "", ""

    def scan(
        self,
        pr: PullRequest,
        diff: str,
        backend_config: LLMBackendConfig | None,
    ) -> list[ReviewFinding]:
        """Scan the PR and file a SecurityReport on a bad verdict.

        Returns an informational ReviewFinding only when something was
        detected, so the operator sees a breadcrumb on the PR detail page
        pointing at the security tab.
        """
        from franktheunicorn.review.backends import get_backend
        from franktheunicorn.review.backends.base import BaseLLMBackend
        from franktheunicorn.security.malicious_prompt import assess
        from franktheunicorn.security.pr_prefilter import file_security_report

        backend: BaseLLMBackend | None = None
        if backend_config is not None:
            candidate = get_backend(backend_config)
            if isinstance(candidate, BaseLLMBackend):
                backend = candidate

        text = "\n\n".join(part for part in (pr.body or "", diff) if part)
        verdict = assess(text, backend, pr_title=pr.title, pr_number=pr.number)

        if not verdict.is_bad:
            return []

        try:
            file_security_report(pr, diff, verdict)
        except Exception:
            logger.exception(
                "Failed to file security report for PR #%d malicious-prompt hit", pr.number
            )

        hit_summary = ", ".join(h.pattern_name for h in verdict.regex_hits) or "(LLM only)"
        body = (
            f"Pre-filter flagged this PR as **{verdict.verdict}** for prompt-injection. "
            f"Filed in the security tab. Regex hits: {hit_summary}."
        )
        if verdict.llm_reasoning:
            body += f"\n\nReasoning: {verdict.llm_reasoning}"

        severity = "critical" if verdict.verdict == "yes" else "important"
        return [
            ReviewFinding(
                title=f"malicious-prompt: {verdict.verdict}",
                body=body,
                severity=severity,
                confidence=0.9 if verdict.verdict == "yes" else 0.6,
            )
        ]
