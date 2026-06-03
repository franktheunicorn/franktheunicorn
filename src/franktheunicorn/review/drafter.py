"""
Review drafter — dispatches to configured LLM backends.

Runs all enabled backends, collects findings from each, deduplicates,
applies tone guard, gates through anti-pattern checks, scores with
the rejection predictor (v1.75), and persists as ReviewDraft rows.
Multiple backends can run in parallel; their findings are combined.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any

from franktheunicorn.core.models import AgentVibe, ReviewDraft
from franktheunicorn.review.antipattern import (
    check_against_anti_patterns,
    record_anti_pattern_matches,
)
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.backends.base import (
    BaseLLMBackend,
    PRContext,
    ReviewFinding,
    ReviewResult,
)
from franktheunicorn.review.context_builder import build_context_strings
from franktheunicorn.review.dedup import deduplicate_findings, merge_source_tags
from franktheunicorn.review.tone_guard import apply_tone_guard_batch
from franktheunicorn.scoring.rejection_predictor import (
    SUPPRESS_THRESHOLD,
    load_predictor_for_project,
    maybe_retrain,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from franktheunicorn.config.models import (
        AgentToolsConfig,
        LLMBackendConfig,
        OperatorConfig,
        ProjectConfig,
    )
    from franktheunicorn.core.models import Project, PullRequest
    from franktheunicorn.review.agent_tools import Tool, ToolRunner
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
    diff_source: str = "",
    sources_per_finding: list[str] | None = None,
) -> list[ReviewDraft]:
    """Convert ReviewFinding objects into ReviewDraft rows.

    Gates through anti-pattern checks, scores with rejection predictor,
    and auto-suppresses high-P(rejection) findings.

    All drafts are created inside a single transaction to avoid partial state
    on worker crash.

    ``source`` is the fallback backend identifier used when
    ``sources_per_finding`` is not supplied (single-backend path or callers
    who don't care about per-finding attribution). When ``sources_per_finding``
    is supplied it must be parallel to ``findings``: each entry is a
    comma-separated list of source tags as produced by
    ``dedup.merge_source_tags`` so deduped multi-backend findings keep
    accurate attribution on ``ReviewDraft.sources``.
    """
    from django.db import transaction

    drafts: list[ReviewDraft] = []

    # Try to load the rejection predictor for this project.
    predictor: RejectionPredictor | None = None
    if project is not None:
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
                tone_guard_applied=tone_guard_applied,
                backend_used=primary_source,
                status="pending",
                sources=finding_sources,
                code_context=code_context,
                rejection_probability=rejection_probability,
                is_auto_suppressed=is_auto_suppressed,
                diff_source=diff_source,
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


def _get_docker_client() -> Any:
    """Lazy-load + ping a Docker client. Returns None if Docker is unavailable.

    Imported lazily (worker-only dependency) so the web container never pulls
    in Docker via the review package.
    """
    try:
        import docker

        client = docker.from_env()  # type: ignore[attr-defined]
        client.ping()
        return client
    except Exception:
        logger.debug("Docker not available for agent tools", exc_info=True)
        return None


@contextmanager
def _tool_session(
    pr: PullRequest,
    project_config: ProjectConfig,
    repo_path: Path | None,
) -> Iterator[tuple[ToolRunner | None, dict[str, Tool], list[dict[str, object]]]]:
    """Yield ``(runner, registry, specs)`` for the agentic tool path.

    Yields ``(None, {}, [])`` when agent tools are disabled or any part of the
    sandbox setup fails (Docker missing, no checkout, image unresolvable, etc.).
    The review then takes the unchanged one-shot path. Never raises during
    setup; the sandbox container is always cleaned up on exit.
    """
    cfg = project_config.agent_tools
    if not cfg.enabled or repo_path is None or not getattr(pr, "head_sha", ""):
        yield None, {}, []
        return

    stack = ExitStack()
    runner: ToolRunner | None = None
    registry: dict[str, Tool] = {}
    specs: list[dict[str, object]] = []
    try:
        from franktheunicorn.review.agent_tools import (
            anthropic_tool_specs,
            build_tool_registry,
        )
        from franktheunicorn.worker.test_image import resolve_image
        from franktheunicorn.worker.test_runner import RESOURCE_TIERS
        from franktheunicorn.worker.test_workspace import pr_branch_workspace
        from franktheunicorn.worker.tool_sandbox import tool_sandbox_session

        docker_client = _get_docker_client()
        if docker_client is None:
            raise RuntimeError("Docker unavailable")

        resources = RESOURCE_TIERS.get(cfg.resource_tier, RESOURCE_TIERS["light"])
        workspace = stack.enter_context(pr_branch_workspace(repo_path, pr.head_sha))
        image = cfg.toolchain_image or resolve_image(
            docker_client,
            project_config.owner,
            project_config.repo,
            project_config.tests,
            workspace,
        )
        runner = stack.enter_context(
            tool_sandbox_session(
                docker_client,
                image,
                workspace,
                resources=resources,
                total_budget_seconds=cfg.time_budget_seconds,
                per_call_timeout=cfg.per_call_timeout_seconds,
                max_output_bytes=cfg.max_output_bytes,
            )
        )
        registry = build_tool_registry(cfg, runner, test_command=project_config.tests.test_command)
        specs = anthropic_tool_specs(registry)
        if not registry:
            logger.info("Agent tools enabled but no tools available; using one-shot review.")
    except Exception:
        logger.info(
            "Agent tool sandbox unavailable for PR #%d; falling back to one-shot review.",
            getattr(pr, "number", 0),
            exc_info=True,
        )
        stack.close()
        runner, registry, specs = None, {}, []

    try:
        yield runner, registry, specs
    finally:
        stack.close()


def _run_single_backend(
    backend_config: LLMBackendConfig,
    diff: str,
    pr_context: PRContext,
    *,
    tool_runner: ToolRunner | None = None,
    tool_registry: dict[str, Tool] | None = None,
    tool_specs: list[dict[str, object]] | None = None,
    tools_cfg: AgentToolsConfig | None = None,
) -> tuple[str, ReviewResult, bool]:
    """Run one backend and return ``(source_name, result, failed)``.

    When a tool runner is supplied and this is the Claude backend, the agentic
    tool-use path is enabled for the call; otherwise behavior is unchanged.
    """
    backend = get_backend(backend_config)
    source = backend_config.provider
    if source == "stub":
        source = "agent"

    if (
        backend_config.provider == "claude"
        and tool_runner is not None
        and tool_specs
        and tools_cfg is not None
        and isinstance(backend, BaseLLMBackend)
    ):
        backend.attach_tools(tool_runner, tool_registry or {}, tool_specs, tools_cfg)

    try:
        if hasattr(backend, "generate_review"):
            result = backend.generate_review(diff, pr_context)
        else:
            result = ReviewResult(findings=backend.generate_findings(diff, pr_context))
    except Exception:
        result = ReviewResult()
        return source, result, True

    return source, result, False


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

    # Collect findings from all backends, and persist each backend's vibe.
    # The vibe key includes the model so two configured backends sharing a
    # provider (e.g. fine-tuned model injection adding a second ollama entry)
    # don't collide on the unique (pull_request, backend) constraint.
    all_findings: list[tuple[str, ReviewFinding]] = []
    failed_backends: list[str] = []
    with _tool_session(pr, project_config, repo_path) as (
        tool_runner,
        tool_registry,
        tool_specs,
    ):
        for backend_config in backend_configs:
            source, result, failed = _run_single_backend(
                backend_config,
                diff,
                pr_context,
                tool_runner=tool_runner,
                tool_registry=tool_registry,
                tool_specs=tool_specs,
                tools_cfg=project_config.agent_tools,
            )
            if failed:
                model_name = backend_config.model or "<default>"
                failed_backends.append(f"{backend_config.provider}/{model_name}")
            if result.overall_vibe:
                vibe_backend = (
                    f"{source}/{backend_config.model}" if backend_config.model else source
                )
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
    # backends fail or produce nothing. Look up by ``backend_used`` (a stable
    # CharField) rather than the JSONField ``sources`` so re-runs don't create
    # duplicate conflict markers if the encoded list ever differs by a byte.
    extra_drafts: list[ReviewDraft] = []
    if pr.mergeable is False:
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
        extra_drafts.append(rebase_draft)

    if not all_findings:
        return extra_drafts

    # Deduplicate across backends.
    raw_findings = [f for _, f in all_findings]
    raw_sources = [s for s, _ in all_findings]
    deduped = deduplicate_findings(raw_findings)
    # Re-build per-deduped source attribution so multi-backend drafts persist
    # all contributors (e.g. "agent,coderabbit") instead of pinning every
    # draft to the first backend that ran.
    deduped_sources = merge_source_tags(raw_findings, raw_sources, deduped)

    # Apply tone guard rewrite pass. apply_tone_guard_batch is 1:1 over the
    # input list, so deduped_sources stays aligned with deduped after this.
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
    # ``source`` is the fallback identifier; per-finding attribution comes
    # from ``sources_per_finding`` so each draft carries the actual list of
    # backends that produced it.
    fallback_source = raw_sources[0] if raw_sources else "agent"
    llm_drafts = create_drafts_from_findings(
        pr,
        deduped,
        source=fallback_source,
        project=pr.project,
        tone_guard_applied=tone_applied,
        diff=diff,
        governance=governance,
        sources_per_finding=deduped_sources,
    )
    return extra_drafts + llm_drafts
