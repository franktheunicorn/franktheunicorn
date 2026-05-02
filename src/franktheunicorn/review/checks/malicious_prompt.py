"""Malicious-prompt sub-check.

Hits are filed as ``SecurityReport`` rows so the operator triages them in
the security tab. The check also returns an informational
``ReviewFinding`` so the standard flow creates a ``ReviewDraft``
breadcrumb on the PR detail page pointing there.
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
    """Pre-filter check for prompt-injection attempts in PR text."""

    name = "malicious-prompt"

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        # Unused — this check uses ``scan`` instead of the standard prompt path.
        return "", ""

    def scan(
        self,
        pr: PullRequest,
        diff: str,
        backend_config: LLMBackendConfig | None,
    ) -> list[ReviewFinding]:
        from franktheunicorn.review.backends import get_backend
        from franktheunicorn.review.backends.base import BaseLLMBackend
        from franktheunicorn.security.malicious_prompt import assess, file_security_report

        backend = None
        if backend_config is not None:
            candidate = get_backend(backend_config)
            if isinstance(candidate, BaseLLMBackend):
                backend = candidate

        text = "\n\n".join(part for part in (pr.title or "", pr.body or "", diff) if part)
        verdict = assess(text, backend)

        if not verdict.is_bad:
            return []

        report_filed = False
        try:
            file_security_report(pr, diff, verdict)
            report_filed = True
        except Exception:
            logger.exception(
                "Failed to file security report for PR #%d malicious-prompt hit", pr.number
            )

        hit_summary = ", ".join(h.pattern_name for h in verdict.regex_hits) or "(LLM only)"
        filing_status = (
            "Filed in the security tab."
            if report_filed
            else "Attempted to file in the security tab, but filing failed."
        )
        body = (
            f"Pre-filter flagged this PR as **{verdict.verdict}** for prompt-injection. "
            f"{filing_status} Regex hits: {hit_summary}."
        )
        if verdict.llm_reasoning:
            body += f"\n\nReasoning: {verdict.llm_reasoning}"

        return [
            ReviewFinding(
                title=f"malicious-prompt: {verdict.verdict}",
                body=body,
                severity="critical" if verdict.verdict == "yes" else "important",
                confidence=0.9 if verdict.verdict == "yes" else 0.6,
            )
        ]
