"""
Review drafter — dispatches to configured LLM backends.

Runs all enabled backends, collects findings from each, gates them
through anti-pattern checks, and persists as ReviewDraft rows.
Multiple backends can run in parallel; their findings are combined.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.core.models import ReviewDraft
from franktheunicorn.review.antipattern import check_against_anti_patterns
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.backends.base import PRContext, ReviewFinding

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
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
        from django.db.models import Q

        from franktheunicorn.core.models import AntiPattern

        aps = AntiPattern.objects.filter(Q(project=pr.project) | Q(project__isnull=True))
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


def _get_pr_diff(pr: PullRequest, diff: str = "") -> str:
    """Return the diff for a PR.

    If ``diff`` is provided (e.g. pre-fetched by the worker via an
    authenticated GitHub client), it is used directly.  Otherwise falls
    back to a minimal placeholder built from ``changed_files`` metadata.
    """
    if diff:
        return diff

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


def _run_single_backend(
    backend_config: LLMBackendConfig,
    diff: str,
    pr_context: PRContext,
) -> tuple[str, list[ReviewFinding]]:
    """Run one backend and return (source_name, findings)."""
    backend = get_backend(backend_config)
    source = backend_config.provider
    if source == "stub":
        source = "agent"

    try:
        findings = backend.generate_findings(diff, pr_context)
    except Exception:
        logger.exception("LLM backend '%s' failed.", backend_config.provider)
        findings = []

    return source, findings


def draft_review(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_config: OperatorConfig | None = None,
) -> list[ReviewDraft]:
    """Generate review drafts for a PR using all configured LLM backends.

    Each backend in ``operator_config.llm_backends`` runs independently.
    Findings from all backends are combined, gated through anti-patterns,
    and stored as ReviewDraft rows with the backend's provider as the source.

    When ``operator_config`` is None or has no backends configured, falls
    back to the stub backend for backwards compatibility.
    """
    if operator_config is None:
        from franktheunicorn.config.models import (
            OperatorConfig as DefaultOperatorConfig,
        )

        operator_config = DefaultOperatorConfig()

    pr_context = build_pr_context(pr, project_config, operator_config)
    diff = _get_pr_diff(pr)

    # Resolve which backends to run.
    backend_configs = operator_config.llm_backends
    if not backend_configs:
        # No backends configured — use stub for demo/test mode.
        from franktheunicorn.config.models import LLMBackendConfig

        backend_configs = [LLMBackendConfig()]

    all_drafts: list[ReviewDraft] = []
    for backend_config in backend_configs:
        source, findings = _run_single_backend(backend_config, diff, pr_context)
        if findings:
            drafts = create_drafts_from_findings(pr, findings, source=source, project=pr.project)
            all_drafts.extend(drafts)

    return all_drafts
