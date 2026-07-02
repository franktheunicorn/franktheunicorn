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

from franktheunicorn.core.models import AgentVibe, ReviewDraft
from franktheunicorn.review.antipattern import (
    check_against_anti_patterns,
    record_anti_pattern_matches,
)
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.backends.base import PRContext, ReviewFinding, ReviewResult
from franktheunicorn.review.context_builder import build_context_strings
from franktheunicorn.review.dedup import (
    deduplicate_findings_with_groups,
    merge_source_tags_from_groups,
)
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


# LLM vocabulary → ReviewDraft.SEVERITY_CHOICES vocabulary.
# The default ReviewFinding.severity is "medium" (which is not a model choice)
# and prompts historically allowed "high"/"medium"/"low". Map those to the
# closest model choice rather than silently downgrading everything to "nit".
_SEVERITY_MAP: dict[str, str] = {
    "critical": "critical",
    "important": "important",
    "high": "important",
    "medium": "nit",
    "nit": "nit",
    "low": "nit",
    "informational": "informational",
    "info": "informational",
    "trivial": "informational",
}


def _coerce_severity(raw_severity: str, finding_title: str = "") -> str:
    """Map an LLM-returned severity string to a valid ReviewDraft choice.

    Unrecognized values fall back to ``"nit"`` and are logged at WARNING
    so prompt regressions surface in the logs instead of being silently
    downgraded.
    """
    key = (raw_severity or "").strip().lower()
    if key in _SEVERITY_MAP:
        return _SEVERITY_MAP[key]
    logger.warning(
        "Unknown LLM severity %r on finding %r — coercing to 'nit'.",
        raw_severity,
        finding_title[:60],
    )
    return "nit"


def ensure_conflict_draft(pr: PullRequest) -> ReviewDraft:
    """Get or create the single rebase-needed draft for a conflicted PR.

    Looks up by ``backend_used`` (a stable CharField) rather than the
    JSONField ``sources`` so re-runs don't create duplicate conflict markers
    if the encoded list ever differs by a byte.
    """
    rebase_draft, _ = ReviewDraft.objects.get_or_create(
        pull_request=pr,
        backend_used="auto-conflict",
        defaults={
            "comment_body": (
                "This PR has merge conflicts with the target branch. "
                "Please rebase onto the latest commit of the base branch before this can be reviewed."
            ),
            "confidence": 1.0,
            "category": "other",
            "severity": "informational",
            "status": "pending",
            "sources": ["auto-conflict"],
            "diff_source": "",
        },
    )
    return rebase_draft


def fetch_linked_issues_context(pr: PullRequest) -> str:
    """Extract issue references from PR title/body and fetch their content.

    Returns formatted issue context or empty string on failure or no refs.
    """
    text = f"{pr.title} {pr.body or ''}"
    if "#" not in text:
        return ""

    try:
        import httpx

        from franktheunicorn.data_access.github.issue_fetcher import IssueFetcher

        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            fetcher = IssueFetcher(client=client)
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


def _derive_category(title: str) -> str:
    """Derive a ReviewDraft category from a finding title.

    Must run against the *original* LLM title — the tone guard replaces
    ``title`` with the pre-rewrite body as a reasoning trace, which destroys
    the ``"security:"``-style prefixes this scan relies on.
    """
    lowered = (title or "").lower()
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
        if cat in lowered:
            return cat
    return "other"


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
                # Find the hunk containing the target line. The last line of a
                # hunk is target_start + target_length - 1 (inclusive).
                for hunk in patched_file:
                    if hunk.target_start <= line_number < hunk.target_start + hunk.target_length:
                        return str(hunk)
                # No hunk contains the line — return nothing rather than an
                # unrelated hunk that would mislead the dashboard/predictor.
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
    diff_source: str = "",
    sources_per_finding: list[str] | None = None,
    categories_per_finding: list[str] | None = None,
    tone_applied_per_finding: list[bool] | None = None,
    rejection_predictor_enabled: bool = False,
) -> list[ReviewDraft]:
    """Convert ReviewFinding objects into ReviewDraft rows.

    Gates through anti-pattern checks and — when the project has explicitly
    enabled the v1.75 rejection predictor — scores findings and
    auto-suppresses high-P(rejection) ones.

    All drafts are created inside a single transaction to avoid partial state
    on worker crash.

    ``source`` is the fallback backend identifier used when
    ``sources_per_finding`` is not supplied (single-backend path or callers
    who don't care about per-finding attribution). When ``sources_per_finding``
    is supplied it must be parallel to ``findings``: each entry is a
    comma-separated list of source tags as produced by
    ``dedup.merge_source_tags_from_groups`` so deduped multi-backend findings
    keep accurate attribution on ``ReviewDraft.sources``.

    ``categories_per_finding`` carries categories derived from the original
    finding titles *before* the tone guard replaced them with reasoning
    traces; without it the category is derived from ``finding.title``.
    ``tone_applied_per_finding`` marks which findings the tone guard actually
    rewrote (the scalar ``tone_guard_applied`` is the fallback for callers
    without per-finding data).
    """
    from django.db import transaction

    drafts: list[ReviewDraft] = []

    # v1.75 feature — only load the predictor when explicitly enabled for the
    # project. It must never activate in a plain v1 deployment (CLAUDE.md:
    # v1.5+ paths are opt-in via config, never the default).
    predictor: RejectionPredictor | None = None
    if project is not None and rejection_predictor_enabled:
        predictor = load_predictor_for_project(project.owner, project.repo)

    with transaction.atomic():
        for idx, finding in enumerate(findings):
            # Resolve attribution for this specific finding. When dedup merged
            # findings from multiple backends, sources_per_finding[idx] holds
            # all contributors as a comma-joined string.
            if sources_per_finding is not None and idx < len(sources_per_finding):
                finding_sources = [
                    s.strip() for s in sources_per_finding[idx].split(",") if s.strip()
                ]
                if not finding_sources:
                    finding_sources = [source]
            else:
                finding_sources = [source]
            primary_source = finding_sources[0]

            # Anti-pattern gate.
            matches = check_against_anti_patterns(finding.body, project)
            if matches:
                record_anti_pattern_matches(matches)
                logger.info(
                    "Suppressed %s finding '%s' — matched anti-pattern(s): %s",
                    primary_source,
                    finding.title[:40],
                    ", ".join(ap.pattern_text[:40] for ap in matches),
                )
                continue

            # Category comes from the original title when the caller derived
            # it before the tone guard replaced titles with reasoning traces.
            if categories_per_finding is not None and idx < len(categories_per_finding):
                category = categories_per_finding[idx]
            else:
                category = _derive_category(finding.title)

            # Extract code context from the diff.
            code_context = _extract_code_context(diff, finding.file_path, finding.line_number)

            # Rejection predictor scoring (v1.75).
            rejection_probability: float | None = None
            is_auto_suppressed = False
            coerced_severity = _coerce_severity(finding.severity, finding.title)
            if predictor is not None:
                features = predictor.extract_features(
                    category=category,
                    severity=coerced_severity,
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

            if tone_applied_per_finding is not None and idx < len(tone_applied_per_finding):
                draft_tone_applied = tone_applied_per_finding[idx]
            else:
                draft_tone_applied = tone_guard_applied

            draft = ReviewDraft.objects.create(
                pull_request=pr,
                file_path=finding.file_path,
                line_number=finding.line_number,
                comment_body=finding.body,
                suggestion=finding.suggestion,
                confidence=finding.confidence,
                severity=coerced_severity,
                category=category,
                reasoning_trace=finding.title,  # original body before tone guard
                tone_guard_applied=draft_tone_applied,
                backend_used=primary_source,
                status="pending",
                sources=finding_sources,
                code_context=code_context,
                rejection_probability=rejection_probability,
                is_auto_suppressed=is_auto_suppressed,
                diff_source=diff_source,
            )
            drafts.append(draft)

    # Auto-retrain check (v1.75) — only when the project opted in.
    if project is not None and rejection_predictor_enabled:
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
    pr: PullRequest | None = None,
) -> tuple[str, ReviewResult, bool]:
    """Run one backend and return ``(source_name, result, failed)``."""
    backend = get_backend(backend_config)
    source = backend_config.provider
    if source == "stub":
        source = "agent"

    try:
        if hasattr(backend, "generate_review"):
            result = backend.generate_review(diff, pr_context)
        else:
            result = ReviewResult(findings=backend.generate_findings(diff, pr_context))
    except Exception:
        result = ReviewResult()
        return source, result, True
    finally:
        # Review calls are the dominant LLM spend — without this the cost
        # widget/digest only ever counted security-triage calls.
        if pr is not None and hasattr(backend, "record_cost"):
            try:
                backend.record_cost(pr.project_id, pr.pk, action_type="review")
            except Exception:
                logger.debug("Failed to record review cost", exc_info=True)

    return source, result, False


def draft_review(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_config: OperatorConfig | None = None,
    *,
    diff: str = "",
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

    ``diff`` is the PR's real unified diff. Callers should always supply it
    (the worker fetches it via ``DiffFetcher``); when empty, a placeholder
    built from ``pr.changed_files`` is used, which tells the backends *which*
    files changed but not *what* changed.

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
    diff = _get_pr_diff(pr, diff)

    # Resolve which backends to run.
    backend_configs = list(operator_config.llm_backends)
    if not backend_configs:
        # No backends configured — use stub for demo/test mode.
        from franktheunicorn.config.models import LLMBackendConfig

        backend_configs = [LLMBackendConfig()]

    # Inject fine-tuned model if configured for this project (v2).
    backend_configs = _maybe_inject_fine_tuned_model(backend_configs, project_config)

    # Collect findings from all backends, and persist each backend's vibe.
    # The vibe key includes the model so two configured backends sharing a
    # provider (e.g. fine-tuned model injection adding a second ollama entry)
    # don't collide on the unique (pull_request, backend) constraint.
    all_findings: list[tuple[str, ReviewFinding]] = []
    failed_backends: list[str] = []
    for backend_config in backend_configs:
        source, result, failed = _run_single_backend(backend_config, diff, pr_context, pr=pr)
        if failed:
            model_name = backend_config.model or "<default>"
            failed_backends.append(f"{backend_config.provider}/{model_name}")
        if result.overall_vibe:
            vibe_backend = f"{source}/{backend_config.model}" if backend_config.model else source
            try:
                AgentVibe.objects.update_or_create(
                    pull_request=pr,
                    backend=vibe_backend,
                    defaults={"vibe_text": result.overall_vibe},
                )
            except Exception:
                logger.debug("Failed to persist agent vibe for %s", vibe_backend, exc_info=True)
        for f in result.findings:
            all_findings.append((source, f))

    if failed_backends:
        logger.warning(
            "LLM backend failures during review dispatch (%d): %s",
            len(failed_backends),
            ", ".join(failed_backends),
        )

    # Always create a rebase-needed draft when GitHub reports merge conflicts.
    # This runs independently of LLM findings so operators see it even when all
    # backends fail or produce nothing.
    extra_drafts: list[ReviewDraft] = []
    if pr.mergeable is False:
        extra_drafts.append(ensure_conflict_draft(pr))

    if not all_findings:
        return extra_drafts

    # Deduplicate across backends, keeping group membership so source
    # attribution can be combined exactly (e.g. "agent,coderabbit").
    raw_findings = [f for _, f in all_findings]
    raw_sources = [s for s, _ in all_findings]
    deduped, dedup_groups = deduplicate_findings_with_groups(raw_findings)
    deduped_sources = merge_source_tags_from_groups(raw_sources, dedup_groups)

    # Derive categories from the original titles now — the tone guard replaces
    # titles with pre-rewrite bodies (reasoning traces), which would destroy
    # the "security:"-style prefixes the category scan relies on.
    categories = [_derive_category(f.title) for f in deduped]

    # Apply tone guard rewrite pass. apply_tone_guard_batch is 1:1 over the
    # input list, so the parallel lists stay aligned with deduped after this.
    tone_backend = backend_configs[0] if backend_configs else None
    is_new_contributor = getattr(pr, "is_new_contributor", False)
    new_contributor_addendum = ""
    if project_config and hasattr(project_config, "new_contributor_addendum"):
        new_contributor_addendum = project_config.new_contributor_addendum

    tone_flags = [False] * len(deduped)
    if tone_backend and tone_backend.provider != "stub":
        deduped, tone_flags = apply_tone_guard_batch(
            deduped,
            pr_context,
            backend_config=tone_backend,
            is_new_contributor=is_new_contributor,
            new_contributor_addendum=new_contributor_addendum,
        )

    # Resolve governance for rejection predictor features.
    governance = getattr(project_config, "governance", "standard")

    # Persist as ReviewDraft rows with anti-pattern gating + rejection scoring.
    # ``source`` is the fallback identifier; per-finding attribution comes
    # from ``sources_per_finding`` so each draft carries the actual list of
    # backends that produced it.
    fallback_source = raw_sources[0] if raw_sources else "agent"
    llm_drafts = create_drafts_from_findings(
        pr,
        deduped,
        source=fallback_source,
        project=pr.project,
        diff=diff,
        governance=governance,
        sources_per_finding=deduped_sources,
        categories_per_finding=categories,
        tone_applied_per_finding=tone_flags,
        rejection_predictor_enabled=getattr(project_config, "rejection_predictor_enabled", False),
    )
    return extra_drafts + llm_drafts
