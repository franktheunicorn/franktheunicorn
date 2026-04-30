"""
Review drafter — dispatches to configured LLM backends.

Runs all enabled backends, collects findings from each, deduplicates,
applies tone guard, gates through anti-pattern checks, scores with
the rejection predictor (v1.75), and persists as ReviewDraft rows.
Multiple backends can run in parallel; their findings are combined.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.core.models import ReviewDraft
from franktheunicorn.review.antipattern import (
    check_against_anti_patterns,
    record_anti_pattern_matches,
)
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.backends.base import PRContext, ReviewFinding
from franktheunicorn.review.context_builder import build_context_strings
from franktheunicorn.review.dedup import deduplicate_findings
from franktheunicorn.review.tone_guard import apply_tone_guard_batch
from franktheunicorn.scoring.rejection_predictor import (
    SUPPRESS_THRESHOLD,
    load_predictor_for_project,
    maybe_retrain,
)

if TYPE_CHECKING:
    from pathlib import Path

    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import Project, PullRequest
    from franktheunicorn.scoring.rejection_predictor import RejectionPredictor

logger = logging.getLogger(__name__)


def fetch_linked_issues_context(pr: PullRequest) -> str:
    """Extract issue references from PR title/body and fetch their content.

    Returns formatted issue context or empty string on failure or no refs.
    """
    text = f"{pr.title} {pr.body or ''}"
    if "#" not in text:
        return ""

    try:
        from franktheunicorn.data_access.github.issue_fetcher import IssueFetcher

        fetcher = IssueFetcher()
        issues = fetcher.fetch_linked_issues(pr.project.owner, pr.project.repo, text)
        if not issues:
            return ""
        return "\n\n".join(issue.to_prompt_context() for issue in issues)
    except Exception:
        logger.debug("Could not fetch linked issues for PR #%d", pr.number, exc_info=True)
        return ""


def build_pr_context(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_config: OperatorConfig,
    *,
    repo_health_context: str = "",
    community_context: str = "",
    jira_context: str = "",
    sentry_context: str = "",
    repo_path: Path | None = None,
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

    from franktheunicorn.personalities import load_personality

    personality = load_personality(operator_config.personality)

    full_file_ctx = ""
    imported_ctx = ""
    try:
        full_file_ctx, imported_ctx = build_context_strings(
            changed_files=pr.changed_files or [],
            repo_path=repo_path,
            config=project_config.context,
        )
    except Exception:
        logger.debug("Failed to build full-file context for PR #%d", pr.number, exc_info=True)

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
        personality_identity=personality.identity if personality else "",
        personality_internal_voice=personality.internal_voice if personality else "",
        personality_external_voice=personality.external_voice if personality else "",
        personality_review_philosophy=(personality.review_philosophy if personality else ""),
        repo_health_context=repo_health_context,
        community_context=community_context,
        jira_context=jira_context,
        sentry_context=sentry_context,
        full_file_context=full_file_ctx,
        imported_modules_context=imported_ctx,
    )


def _get_pr_diff(pr: PullRequest, diff: str = "") -> str:
    """Return pre-fetched diff, or a placeholder from changed_files metadata."""
    if diff:
        return diff
    files: list[str] = pr.changed_files or []
    return "\n".join(f"+++ b/{f}" for f in files) + "\n" if files else "+++ b/unknown_file.py\n"


def _extract_code_context(diff: str, file_path: str, line_number: int | None) -> str:
    """Extract the relevant diff hunk for a finding's file and line.

    Uses unidiff to parse the diff and find the hunk containing the target line.
    Returns the hunk text, or empty string if not found.
    """
    if not diff or not file_path:
        return ""

    try:
        from unidiff import PatchSet  # type: ignore[import-untyped]

        patch = PatchSet(diff)
        for patched_file in patch:
            # Match by filename (strip a/ b/ prefixes).
            # unidiff strips a/b/ prefixes from patched_file.path.
            fname = patched_file.path
            target = file_path.removeprefix("a/").removeprefix("b/")
            if fname == target:
                if line_number is None:
                    # No specific line — return first hunk.
                    if patched_file:
                        return str(patched_file[0])
                    return ""
                # Find the hunk containing the target line.
                for hunk in patched_file:
                    if hunk.target_start <= line_number <= hunk.target_start + hunk.target_length:
                        return str(hunk)
                # If no hunk matches the exact line, return the closest.
                if patched_file:
                    return str(patched_file[0])
                return ""
    except Exception:
        logger.debug("Could not parse diff for code context extraction.")
    return ""


def create_drafts_from_findings(
    pr: PullRequest,
    findings: list[ReviewFinding],
    source: str,
    project: Project | None = None,
    *,
    tone_guard_applied: bool = False,
    diff: str = "",
    governance: str = "standard",
) -> list[ReviewDraft]:
    """Convert ReviewFinding objects into ReviewDraft rows.

    Gates through anti-pattern checks, scores with rejection predictor,
    and auto-suppresses high-P(rejection) findings.

    All drafts are created inside a single transaction to avoid partial state
    on worker crash.
    """
    from django.db import transaction

    drafts: list[ReviewDraft] = []

    # Try to load the rejection predictor for this project.
    predictor: RejectionPredictor | None = None
    if project is not None:
        predictor = load_predictor_for_project(project.owner, project.repo)

    with transaction.atomic():
        for finding in findings:
            # Anti-pattern gate.
            matches = check_against_anti_patterns(finding.body, project)
            if matches:
                record_anti_pattern_matches(matches)
                logger.info(
                    "Suppressed %s finding '%s' — matched anti-pattern(s): %s",
                    source,
                    finding.title[:40],
                    ", ".join(ap.pattern_text[:40] for ap in matches),
                )
                continue

            # Map severity string to a valid category if present in the finding title.
            category = "other"
            for cat in (
                "correctness",
                "style",
                "security-context",
                "security",
                "test-coverage",
                "architectural",
                "naming",
                "suggested-change",
                "moderation",
                "issue-link",
            ):
                if cat in (finding.title or "").lower():
                    category = cat
                    break

            # Extract code context from the diff.
            code_context = _extract_code_context(diff, finding.file_path, finding.line_number)

            # Rejection predictor scoring (v1.75).
            rejection_probability: float | None = None
            is_auto_suppressed = False
            if predictor is not None:
                features = predictor.extract_features(
                    category=category,
                    severity=finding.severity
                    if finding.severity in ("critical", "important", "nit", "informational")
                    else "nit",
                    file_path=finding.file_path,
                    comment_body=finding.body,
                    code_context=code_context,
                    governance=governance,
                    is_new_contributor=pr.is_new_contributor,
                    is_ai_pr=pr.likely_ai_generated,
                    additions=pr.additions,
                    deletions=pr.deletions,
                    project_id=pr.project_id,
                )
                rejection_probability = predictor.predict_rejection(features)
                if rejection_probability > SUPPRESS_THRESHOLD:
                    is_auto_suppressed = True
                    logger.info(
                        "Auto-suppressed finding '%s' — P(rejection)=%.2f",
                        finding.title[:40],
                        rejection_probability,
                    )

            draft = ReviewDraft.objects.create(
                pull_request=pr,
                file_path=finding.file_path,
                line_number=finding.line_number,
                comment_body=finding.body,
                suggestion=finding.suggestion,
                confidence=finding.confidence,
                severity=finding.severity
                if finding.severity in ("critical", "important", "nit", "informational")
                else "nit",
                category=category,
                reasoning_trace=finding.title,  # original body before tone guard
                tone_guard_applied=tone_guard_applied,
                backend_used=source,
                status="pending",
                sources=[source],
                code_context=code_context,
                rejection_probability=rejection_probability,
                is_auto_suppressed=is_auto_suppressed,
            )
            drafts.append(draft)

    # Auto-retrain check (v1.75).
    if project is not None:
        try:
            maybe_retrain(project.pk, project.owner, project.repo)
        except Exception:
            logger.debug("Rejection model retrain check failed.", exc_info=True)

    return drafts


def _maybe_inject_fine_tuned_model(
    backend_configs: list[LLMBackendConfig],
    project_config: ProjectConfig,
) -> list[LLMBackendConfig]:
    """Inject a fine-tuned model backend if the project has one enabled.

    The fine-tuned model is inserted at the beginning of the backend list,
    acting as the first-pass reviewer. Other backends still run for coverage.
    """
    ft_config = project_config.fine_tuned_model
    if not ft_config.enabled or not ft_config.model:
        return backend_configs

    from franktheunicorn.config.models import LLMBackendConfig

    ft_backend = LLMBackendConfig(
        provider=ft_config.provider,
        model=ft_config.model,
        base_url=ft_config.endpoint,
        temperature=0.3,
    )

    # Insert at the beginning (first-pass slot).
    return [ft_backend, *backend_configs]


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
    *,
    repo_health_context: str = "",
    community_context: str = "",
    jira_context: str = "",
    sentry_context: str = "",
    repo_path: Path | None = None,
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

    pr_context = build_pr_context(
        pr,
        project_config,
        operator_config,
        repo_health_context=repo_health_context,
        community_context=community_context,
        jira_context=jira_context,
        sentry_context=sentry_context,
        repo_path=repo_path,
    )
    diff = _get_pr_diff(pr)

    # Resolve which backends to run.
    backend_configs = list(operator_config.llm_backends)
    if not backend_configs:
        # No backends configured — use stub for demo/test mode.
        from franktheunicorn.config.models import LLMBackendConfig

        backend_configs = [LLMBackendConfig()]

    # Inject fine-tuned model if configured for this project (v2).
    backend_configs = _maybe_inject_fine_tuned_model(backend_configs, project_config)

    # Collect findings from all backends.
    all_findings: list[tuple[str, ReviewFinding]] = []
    for backend_config in backend_configs:
        source, findings = _run_single_backend(backend_config, diff, pr_context)
        for f in findings:
            all_findings.append((source, f))

    if not all_findings:
        return []

    # Deduplicate across backends.
    raw_findings = [f for _, f in all_findings]
    deduped = deduplicate_findings(raw_findings)

    # Apply tone guard rewrite pass.
    tone_backend = backend_configs[0] if backend_configs else None
    is_new_contributor = getattr(pr, "is_new_contributor", False)
    new_contributor_addendum = ""
    if project_config and hasattr(project_config, "new_contributor_addendum"):
        new_contributor_addendum = project_config.new_contributor_addendum

    tone_applied = False
    if tone_backend and tone_backend.provider != "stub":
        deduped = apply_tone_guard_batch(
            deduped,
            pr_context,
            backend_config=tone_backend,
            is_new_contributor=is_new_contributor,
            new_contributor_addendum=new_contributor_addendum,
        )
        tone_applied = True

    # Resolve governance for rejection predictor features.
    governance = getattr(project_config, "governance", "standard")

    # Persist as ReviewDraft rows with anti-pattern gating + rejection scoring.
    source_name = all_findings[0][0] if all_findings else "agent"
    return create_drafts_from_findings(
        pr,
        deduped,
        source=source_name,
        project=pr.project,
        tone_guard_applied=tone_applied,
        diff=diff,
        governance=governance,
    )
