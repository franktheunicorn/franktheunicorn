"""PR pre-filter: scan a pull request's text for malicious prompts.

When the malicious-prompt detector returns a "yes" or "maybe" verdict,
this module files a ``SecurityReport`` so the operator sees the PR in
the security tab. Already-reported PRs are deduped on the same scan day.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.security.malicious_prompt import (
    MaliciousPromptVerdict,
    assess,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig
    from franktheunicorn.core.models import PullRequest, SecurityReport
    from franktheunicorn.review.backends.base import BaseLLMBackend

logger = logging.getLogger(__name__)


def _get_backend(operator_config: OperatorConfig) -> BaseLLMBackend | None:
    """Return the first configured LLM backend, or None."""
    if not operator_config.llm_backends:
        return None

    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.backends.base import BaseLLMBackend

    backend = get_backend(operator_config.llm_backends[0])
    if not isinstance(backend, BaseLLMBackend):
        return None
    return backend


def _format_report_text(
    pr: PullRequest,
    diff: str,
    verdict: MaliciousPromptVerdict,
) -> str:
    """Compose the ``raw_text`` body for a SecurityReport."""
    lines: list[str] = [
        f"Auto-detected malicious prompt in PR #{pr.number}: {pr.title}",
        f"Author: {pr.author}",
        f"URL: {pr.url}",
        f"Verdict: {verdict.verdict} (LLM consulted: {verdict.llm_called})",
        "",
        "## Regex pre-filter hits",
    ]
    if verdict.regex_hits:
        for hit in verdict.regex_hits:
            lines.append(f"- {hit.pattern_name} ({hit.severity}): {hit.snippet}")
    else:
        lines.append("(none)")

    if verdict.llm_reasoning:
        lines.extend(["", "## LLM reasoning", verdict.llm_reasoning])

    if pr.body:
        lines.extend(["", "## PR description", pr.body[:5000]])

    if diff:
        lines.extend(["", "## Diff (truncated)", diff[:10000]])

    return "\n".join(lines)


def _marker_for_pr(pr: PullRequest) -> str:
    """Return the dedupe marker for a PR.

    Wrapped in brackets so substring lookups are unambiguous —
    e.g. ``[prefilter:pr-1-2]`` does not collide with ``[prefilter:pr-1-20]``.
    """
    return f"[prefilter:pr-{pr.project_id}-{pr.number}]"


def _existing_report_for_pr(pr: PullRequest) -> SecurityReport | None:
    """Look for an existing auto-generated report for this PR."""
    from franktheunicorn.core.models import SecurityReport

    return SecurityReport.objects.filter(
        project=pr.project, operator_notes__contains=_marker_for_pr(pr)
    ).first()


def file_security_report(
    pr: PullRequest,
    diff: str,
    verdict: MaliciousPromptVerdict,
) -> SecurityReport | None:
    """Create (or update) a SecurityReport for a malicious-prompt hit.

    Idempotent: returns the existing report if one already exists for this PR.
    """
    from franktheunicorn.core.models import SecurityReport

    if not verdict.is_bad:
        return None

    existing = _existing_report_for_pr(pr)
    if existing is not None:
        return existing

    severity = "high" if verdict.verdict == "yes" else "medium"
    title = f"Malicious prompt detected in PR #{pr.number}"

    return SecurityReport.objects.create(
        project=pr.project,
        title=title[:500],
        raw_text=_format_report_text(pr, diff, verdict),
        source="paste",
        reporter_name="franktheunicorn (auto pre-filter)",
        status="new",
        assessed_severity=severity,
        triage_summary=verdict.llm_reasoning or "Regex pre-filter hits only.",
        operator_notes=_marker_for_pr(pr),
    )


def scan_pull_request(
    pr: PullRequest,
    diff: str,
    operator_config: OperatorConfig,
) -> MaliciousPromptVerdict:
    """Run the malicious-prompt detector against a PR's body + diff.

    On a bad verdict, files a SecurityReport. Always returns the verdict so
    callers can log or act on it.
    """
    text = "\n\n".join(part for part in (pr.body or "", diff) if part)
    backend = _get_backend(operator_config)

    verdict = assess(text, backend, pr_title=pr.title, pr_number=pr.number)

    if verdict.is_bad:
        try:
            file_security_report(pr, diff, verdict)
        except Exception:
            logger.exception(
                "Failed to file security report for PR #%d malicious-prompt hit", pr.number
            )

    return verdict
