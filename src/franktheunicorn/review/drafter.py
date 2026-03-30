"""
Review drafter — dispatches to the configured LLM backend.

Generates ReviewFinding objects via the selected backend, gates them
through anti-pattern checks, and persists as ReviewDraft rows.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.core.models import ReviewDraft
from franktheunicorn.review.antipattern import check_against_anti_patterns
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.backends.base import PRContext, ReviewFinding

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import Project, PullRequest

logger = logging.getLogger(__name__)


def build_pr_context(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_config: OperatorConfig,
) -> PRContext:
    """Bundle PR + config data into a PRContext for the LLM."""
    anti_patterns: list[str] = []
    try:
        from franktheunicorn.core.models import AntiPattern

        aps = AntiPattern.objects.filter(project__in=[pr.project, None])
        anti_patterns = [ap.pattern_text for ap in aps]
    except Exception:
        logger.debug("Could not load anti-patterns for prompt context.")

    return PRContext(
        pr_title=pr.title,
        pr_body=pr.body or "",
        pr_author=pr.author,
        pr_number=pr.number,
        project_name=pr.project.full_name,
        review_context=project_config.review_context,
        review_style=operator_config.review_style,
        tone=project_config.tone,
        test_expectations=project_config.test_expectations,
        governance=project_config.governance,
        anti_patterns=anti_patterns,
    )


def _get_pr_diff(pr: PullRequest) -> str:
    """Retrieve the diff for a PR.

    Uses the stored changed_files list to build a minimal placeholder diff
    when a real diff is not available (e.g. in mock mode). In production,
    the worker should fetch the actual diff via the GitHub client and pass
    it through.
    """
    # If the PR has a diff_url, try fetching it.
    if pr.diff_url:
        try:
            import httpx

            resp = httpx.get(pr.diff_url, timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                return resp.text
        except Exception:
            logger.debug("Could not fetch diff from %s", pr.diff_url)

    # Fallback: build a stub diff from changed_files metadata.
    files = pr.changed_files or []
    if not files:
        return "+++ b/unknown_file.py\n"
    return "\n".join(f"+++ b/{f}" for f in files) + "\n"


def create_drafts_from_findings(
    pr: PullRequest,
    findings: list[ReviewFinding],
    source: str,
    project: Project | None = None,
) -> list[ReviewDraft]:
    """Convert ReviewFinding objects into ReviewDraft rows with anti-pattern gating."""
    drafts: list[ReviewDraft] = []

    for finding in findings:
        # Anti-pattern gate.
        matches = check_against_anti_patterns(finding.body, project)
        if matches:
            logger.info(
                "Suppressed %s finding '%s' — matched anti-pattern(s): %s",
                source,
                finding.title[:40],
                ", ".join(ap.pattern_text[:40] for ap in matches),
            )
            continue

        draft = ReviewDraft.objects.create(
            pull_request=pr,
            file_path=finding.file_path,
            line_number=finding.line_number,
            comment_body=finding.body,
            suggestion=finding.suggestion,
            confidence=finding.confidence,
            status="pending",
            source=source,
        )
        drafts.append(draft)

    return drafts


def draft_review(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_config: OperatorConfig | None = None,
) -> list[ReviewDraft]:
    """Generate review drafts for a PR using the configured LLM backend.

    When ``operator_config`` is None, falls back to the stub backend
    for backwards compatibility with existing callers.
    """
    if operator_config is None:
        from franktheunicorn.config.models import (
            OperatorConfig as DefaultOperatorConfig,
        )

        operator_config = DefaultOperatorConfig()

    backend = get_backend(operator_config.llm)
    pr_context = build_pr_context(pr, project_config, operator_config)
    diff = _get_pr_diff(pr)

    try:
        findings = backend.generate_findings(diff, pr_context)
    except Exception:
        logger.exception("LLM backend '%s' failed.", operator_config.llm.provider)
        findings = []

    if not findings:
        return []

    source = operator_config.llm.provider
    if source == "stub":
        source = "agent"  # Keep backwards-compatible source name.

    return create_drafts_from_findings(pr, findings, source=source, project=pr.project)
