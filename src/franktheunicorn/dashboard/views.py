"""
Dashboard views — server-rendered HTML with htmx interactivity.

Function-based views. No SPA, no React. htmx for all dynamic updates.
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from franktheunicorn.core.models import (
    AgentFeedback,
    AgentVibe,
    AntiPattern,
    CostRecord,
    DependencyChange,
    OperatorAction,
    Project,
    PullRequest,
    ReviewDraft,
    SecurityReport,
    TestRun,
)

logger = logging.getLogger(__name__)

# Queue definitions for the tab bar.
QUEUE_TABS: list[dict[str, str]] = [
    {"key": "review", "label": "Review"},
    {"key": "your-prs", "label": "Your PRs"},
    {"key": "ai-generated", "label": "AI-Generated"},
    {"key": "new-contributor", "label": "New Contributors"},
    {"key": "consider-closing", "label": "Consider Closing"},
    {"key": "needs-triage", "label": "Needs Triage"},
    {"key": "wip", "label": "WIP"},
]

# Valid project type values and their human-readable labels.
VALID_PROJECT_TYPES: frozenset[str] = frozenset({"asf", "personal", "org"})
PROJECT_TYPE_LABELS: dict[str, str] = {"asf": "ASF", "personal": "Personal", "org": "Organization"}


def _get_workspace_projects(request: HttpRequest) -> list[str] | None:
    """Get project full_names for the active workspace from cookie.

    Returns None for "all" workspace (no filtering).
    """
    workspace = request.COOKIES.get("workspace", "all")
    if workspace == "all":
        return None
    try:
        from django.conf import settings

        from franktheunicorn.config.loader import load_operator_config

        config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
        workspaces = getattr(config, "workspaces", {})
        if workspace in workspaces:
            ws = workspaces[workspace]
            projects = ws.get("projects", "*") if isinstance(ws, dict) else "*"
            if projects != "*":
                return list(projects)
    except Exception:
        pass
    return None


def _build_workspace_q(workspace_projects: list[str]) -> Q:
    """Build a Q filter for a list of 'owner/repo' project full names."""
    q = Q()
    for full_name in workspace_projects:
        parts = full_name.split("/", 1)
        if len(parts) == 2:
            q |= Q(project__owner=parts[0], project__repo=parts[1])
    return q


def _parse_project_slug(project: str) -> tuple[str, str] | None:
    """Parse an ``owner/repo`` project slug, returning ``(owner, repo)`` or ``None``."""
    if not project:
        return None
    parts = project.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def index(request: HttpRequest) -> HttpResponse:
    """Main dashboard: list of PRs sorted by interest score with queue tabs.

    Supports optional GET filters:
    - ``queue``: one of the QUEUE_TABS keys (default ``review``)
    - ``project_type``: one of ``asf``, ``personal``, ``org`` (default all)
    - ``project``: an ``owner/repo`` string to narrow to a single project (default all)
    """
    queue = request.GET.get("queue", "review")
    project_type = request.GET.get("project_type", "")
    project = request.GET.get("project", "")
    workspace_projects = _get_workspace_projects(request)

    prs = (
        PullRequest.objects.select_related("project")
        .filter(state="open", queue=queue)
        .order_by("-interest_score", "-github_updated_at")
    )

    if workspace_projects is not None:
        prs = prs.filter(_build_workspace_q(workspace_projects))

    # Apply project-type filter (ignore unknown values).
    active_project_type = project_type if project_type in VALID_PROJECT_TYPES else ""
    if active_project_type:
        prs = prs.filter(project__project_type=active_project_type)

    # Apply specific-project filter (ignore malformed values).
    parsed_project = _parse_project_slug(project)
    active_project = project if parsed_project is not None else ""
    if parsed_project is not None:
        prs = prs.filter(project__owner=parsed_project[0], project__repo=parsed_project[1])

    # Slice to displayed rows first, then fetch findings counts in a single
    # grouped query keyed by those PR ids. Avoids a JOIN + GROUP BY across all
    # open PRs (which would block index use for the order_by).
    pr_list: list[PullRequest] = list(prs[:100])
    if pr_list:
        pr_ids = [pr.pk for pr in pr_list]
        finding_counts = dict(
            ReviewDraft.objects.filter(ReviewDraft.line_finding_q())
            .filter(pull_request_id__in=pr_ids)
            .values("pull_request_id")
            .annotate(c=Count("id"))
            .values_list("pull_request_id", "c")
        )
        # Fetch the latest completed test run verdict per PR.
        # Order by created_at DESC and pick the first per PR (SQLite-compatible).
        latest_verdicts: dict[int, str] = {}
        for run_pr_id, verdict in (
            TestRun.objects.filter(
                pull_request_id__in=pr_ids,
                status="completed",
                differential_verdict__isnull=False,
            )
            .order_by("-created_at")
            .values_list("pull_request_id", "differential_verdict")
        ):
            if run_pr_id not in latest_verdicts and verdict is not None:
                latest_verdicts[run_pr_id] = verdict
        for pr in pr_list:
            pr.findings_count = finding_counts.get(pr.pk, 0)  # type: ignore[attr-defined]
            pr.latest_test_verdict = latest_verdicts.get(pr.pk)  # type: ignore[attr-defined]

    # Count PRs per queue for tab badges (respects the same project/type filters).
    base_qs = PullRequest.objects.filter(state="open")
    if workspace_projects is not None:
        base_qs = base_qs.filter(_build_workspace_q(workspace_projects))
    if active_project_type:
        base_qs = base_qs.filter(project__project_type=active_project_type)
    if parsed_project is not None:
        base_qs = base_qs.filter(project__owner=parsed_project[0], project__repo=parsed_project[1])
    queue_counts: dict[str, int] = {
        tab["key"]: base_qs.filter(queue=tab["key"]).count() for tab in QUEUE_TABS
    }
    queue_tabs_with_counts = [
        {**tab, "count": queue_counts.get(tab["key"], 0)} for tab in QUEUE_TABS
    ]

    # Build available filter options from enabled projects only.
    enabled_projects_qs = Project.objects.filter(enabled=True)
    available_type_keys = list(
        enabled_projects_qs.values_list("project_type", flat=True)
        .distinct()
        .order_by("project_type")
    )
    available_project_types = [
        {"key": k, "label": PROJECT_TYPE_LABELS.get(k, k)} for k in available_type_keys
    ]
    # Narrow project list to the selected type so the second selector is contextual.
    projects_qs = enabled_projects_qs.order_by("owner", "repo")
    if active_project_type:
        projects_qs = projects_qs.filter(project_type=active_project_type)
    available_projects = list(projects_qs.values("owner", "repo"))

    return render(
        request,
        "dashboard/pr_list.html",
        {
            "pull_requests": pr_list,
            "queue_tabs": queue_tabs_with_counts,
            "active_queue": queue,
            "queue_counts": queue_counts,
            "active_project_type": active_project_type,
            "active_project": active_project,
            "available_project_types": available_project_types,
            "available_projects": available_projects,
        },
    )


def set_workspace(request: HttpRequest) -> HttpResponse:
    """Set the active workspace via cookie."""
    workspace = request.POST.get("workspace", "all")
    response = redirect("dashboard:index")
    response.set_cookie("workspace", workspace, max_age=86400 * 365)
    return response


def _draft_source_key(draft: ReviewDraft) -> str:
    """Return the primary source identifier for a draft.

    Prefers the first entry in ``draft.sources``; falls back to
    ``backend_used`` and then ``"unknown"``.
    """
    if draft.sources:
        return str(draft.sources[0])
    if draft.backend_used:
        return draft.backend_used
    return "unknown"


# Maximum number of characters to show for a finding's body snippet in the
# agent run summary table.
_BODY_SNIPPET_MAX_LEN = 120


def build_agent_run_summary(
    pr: PullRequest,
    operator_config: OperatorConfig,
    project_config: ProjectConfig | None,
) -> list[dict[str, object]]:
    """Build a structured summary of which agents ran (or were configured) for a PR.

    Returns a list of dicts, one per agent, ordered by: LLM backends first,
    then CodeRabbit, then LLM checks, then shepherding, then any extra sources
    found in the database that were not part of the configured set.

    Each dict has the following keys:

    - ``source``: internal source key (matches ``ReviewDraft.sources[0]``)
    - ``display_name``: human-readable agent name
    - ``did_run``: True if at least one draft was produced by this agent
    - ``total``: total finding count (including auto-suppressed)
    - ``active``: non-suppressed finding count
    - ``suppressed``: auto-suppressed count
    - ``pending`` / ``accepted`` / ``edited`` / ``rejected`` / ``posted`` /
      ``recalled``: per-status counts for non-suppressed drafts
    - ``findings``: list of line-level dicts with ``file_path``, ``line_number``,
      ``severity``, ``category``, ``body_snippet``, ``is_suppressed``, ``status``
    """
    from collections import defaultdict

    # Fetch all drafts (including suppressed) for this PR in a single query.
    all_drafts = list(
        ReviewDraft.objects.filter(pull_request=pr).order_by("file_path", "line_number")
    )

    # Group drafts by their primary source key.
    source_drafts: dict[str, list[ReviewDraft]] = defaultdict(list)
    for draft in all_drafts:
        source_drafts[_draft_source_key(draft)].append(draft)

    # Build the ordered list of configured (expected) agents.
    configured: list[tuple[str, str]] = []  # (source_key, display_name)

    # 1. LLM backends from operator config.
    backends = list(operator_config.llm_backends)
    if not backends:
        # Stub fallback — used when no backends are configured.
        configured.append(("agent", "Stub Agent"))
    else:
        for backend in backends:
            source_key = "agent" if backend.provider == "stub" else backend.provider
            display = backend.provider.title()
            if backend.model:
                display += f" ({backend.model})"
            configured.append((source_key, display))

    # 2. CodeRabbit (when enabled).
    if operator_config.coderabbit.enabled:
        configured.append(("coderabbit", "CodeRabbit"))

    # 3. LLM sub-checks from project config.
    if project_config:
        for check_name in project_config.llm_checks:
            pretty = check_name.replace("-", " ").title()
            configured.append((f"check:{check_name}", f"Check: {pretty}"))

    # 4. Shepherding — only relevant for the operator's own PRs.
    if pr.is_operator_pr:
        configured.append(("shepherding", "Shepherding"))

    # 5. Any sources present in the DB that weren't in the configured set
    #    (e.g. a backend removed from config after it already ran, or copypasta).
    configured_keys = {key for key, _ in configured}
    for src_key in source_drafts:
        if src_key and src_key not in configured_keys and src_key != "unknown":
            pretty = src_key.replace("check:", "Check: ").replace("-", " ").title()
            configured.append((src_key, pretty))

    # Build one summary entry per agent.
    _status_keys = ("pending", "accepted", "edited", "rejected", "posted", "recalled")

    summary = []
    for source_key, display_name in configured:
        drafts = source_drafts.get(source_key, [])
        did_run = bool(drafts)

        status_counts: dict[str, int] = dict.fromkeys(_status_keys, 0)
        suppressed_count = 0
        findings_list: list[dict[str, object]] = []

        for d in drafts:
            if d.is_auto_suppressed:
                suppressed_count += 1
            else:
                bucket = d.status if d.status in status_counts else "pending"
                status_counts[bucket] += 1

            findings_list.append(
                {
                    "file_path": d.file_path,
                    "line_number": d.line_number,
                    "severity": d.severity,
                    "category": d.category,
                    "body_snippet": (d.edited_body or d.comment_body or "")[:_BODY_SNIPPET_MAX_LEN],
                    "is_suppressed": d.is_auto_suppressed,
                    "status": d.status,
                }
            )

        diff_source = next((d.diff_source for d in drafts if d.diff_source), "")

        summary.append(
            {
                "source": source_key,
                "display_name": display_name,
                "did_run": did_run,
                "total": len(drafts),
                "active": len(drafts) - suppressed_count,
                "suppressed": suppressed_count,
                **status_counts,
                "findings": findings_list,
                "diff_source": diff_source,
            }
        )

    return summary


def _adjacent_prs(pr: PullRequest) -> tuple[PullRequest | None, PullRequest | None]:
    """Return (prev_pr, next_pr) in the same queue, ordered by -interest_score, -github_updated_at.

    "Next" means the next PR the operator would review (lower score); "prev" is higher score.
    Both are None when there is no adjacent entry.
    """
    same_queue = PullRequest.objects.filter(state="open", queue=pr.queue).order_by(
        "-interest_score", "-github_updated_at"
    )
    ids: list[int] = list(same_queue.values_list("pk", flat=True)[:200])
    if pr.pk not in ids:
        return None, None
    idx = ids.index(pr.pk)
    prev_pr = PullRequest.objects.filter(pk=ids[idx - 1]).first() if idx > 0 else None
    next_pr = PullRequest.objects.filter(pk=ids[idx + 1]).first() if idx < len(ids) - 1 else None
    return prev_pr, next_pr


def pr_detail(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Detail view for a single PR showing drafts and score breakdown."""
    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)
    drafts = (
        ReviewDraft.objects.filter(
            pull_request=pr,
            is_auto_suppressed=False,
        )
        .select_related("pull_request")
        .order_by("file_path", "line_number")
    )
    suppressed_drafts = (
        ReviewDraft.objects.filter(
            pull_request=pr,
            is_auto_suppressed=True,
        )
        .select_related("pull_request")
        .order_by("file_path", "line_number")
    )
    dep_changes = DependencyChange.objects.filter(pull_request=pr).order_by("package_name")
    test_runs = TestRun.objects.filter(pull_request=pr).order_by("-created_at")
    agent_vibes = AgentVibe.objects.filter(pull_request=pr).order_by("backend")

    # Check if agent feedback is enabled (v1.25).
    feedback_enabled = _is_agent_feedback_enabled()

    # Load config — used for personality name and agent run summary.
    from franktheunicorn.config.loader import get_operator_config, get_project_config

    operator_config = get_operator_config()
    personality_name = operator_config.personality
    project_config = get_project_config(pr.project.full_name)

    # v1.5: Separate CodeRabbit-sourced findings.
    coderabbit_drafts = [d for d in drafts if any("coderabbit" in s for s in (d.sources or []))]
    agent_drafts = [d for d in drafts if not any("coderabbit" in s for s in (d.sources or []))]

    # v1.5: External context (JIRA, community, Sentry).
    jira_context = pr.jira_cache if pr.jira_cache else None
    community_context = pr.community_context_cache if pr.community_context_cache else None
    sentry_context = pr.sentry_context_cache if pr.sentry_context_cache else None

    # Agent run summary: which agents ran, their stats, and which didn't.
    agent_run_summary = build_agent_run_summary(pr, operator_config, project_config)

    prev_pr, next_pr = _adjacent_prs(pr)

    return render(
        request,
        "dashboard/pr_detail.html",
        {
            "pr": pr,
            "drafts": drafts,
            "suppressed_drafts": suppressed_drafts,
            "agent_drafts": agent_drafts,
            "coderabbit_drafts": coderabbit_drafts,
            "agent_vibes": agent_vibes,
            "dep_changes": dep_changes,
            "test_runs": test_runs,
            "feedback_enabled": feedback_enabled,
            "personality_name": personality_name,
            "jira_context": jira_context,
            "community_context": community_context,
            "sentry_context": sentry_context,
            "agent_run_summary": agent_run_summary,
            "prev_pr": prev_pr,
            "next_pr": next_pr,
        },
    )


# --- Finding actions (htmx) ---


def _action_type_for_draft(draft: ReviewDraft, action: str) -> str:
    """Return the appropriate action type based on draft source."""
    if "shepherding" in (draft.sources or []):
        return f"{action}_shepherd"
    return f"{action}_draft"


@require_POST
def approve_draft(request: HttpRequest, draft_id: int) -> HttpResponse:
    """Approve a draft finding."""
    draft = get_object_or_404(ReviewDraft, pk=draft_id)
    draft.status = "accepted"
    draft.is_auto_suppressed = False
    draft.save(update_fields=["status", "is_auto_suppressed", "updated_at"])

    OperatorAction.objects.create(
        action_type=_action_type_for_draft(draft, "accept"),
        review_draft=draft,
        pull_request=draft.pull_request,
    )
    return render(request, "dashboard/_draft_item.html", {"draft": draft})


@require_POST
def reject_draft(request: HttpRequest, draft_id: int) -> HttpResponse:
    """Reject a draft finding with optional reason."""
    draft = get_object_or_404(ReviewDraft, pk=draft_id)
    reason = request.POST.get("reason", "")
    draft.status = "rejected"
    draft.rejection_reason = reason
    draft.save(update_fields=["status", "rejection_reason", "updated_at"])

    OperatorAction.objects.create(
        action_type=_action_type_for_draft(draft, "reject"),
        review_draft=draft,
        pull_request=draft.pull_request,
        notes=reason,
    )

    # Auto-suggest anti-pattern from rejected draft.
    if reason:
        from franktheunicorn.review.antipattern import record_anti_pattern

        record_anti_pattern(
            pattern_text=reason,
            description=f"Auto-suggested from rejected draft #{draft.pk}",
            project=draft.pull_request.project,
        )

    return render(request, "dashboard/_draft_item.html", {"draft": draft})


@require_POST
def edit_draft(request: HttpRequest, draft_id: int) -> HttpResponse:
    """Edit a draft finding's body."""
    draft = get_object_or_404(ReviewDraft, pk=draft_id)
    new_body = request.POST.get("edited_body", "")
    if new_body and new_body != draft.comment_body:
        draft.status = "edited"
        draft.edited_body = new_body
        draft.save(update_fields=["status", "edited_body", "updated_at"])

        OperatorAction.objects.create(
            action_type=_action_type_for_draft(draft, "edit"),
            review_draft=draft,
            pull_request=draft.pull_request,
        )
    return render(request, "dashboard/_draft_item.html", {"draft": draft})


@require_POST
def recall_draft(request: HttpRequest, draft_id: int) -> HttpResponse:
    """Recall (delete) a posted comment from GitHub within the recall window."""
    draft = get_object_or_404(ReviewDraft, pk=draft_id)
    if draft.status != "posted" or not draft.forge_comment_id:
        return HttpResponse(
            '<div class="recall-result" style="color: #c00;">Cannot recall: not posted.</div>'
        )
    try:
        from django.conf import settings

        from franktheunicorn.backends.github import GitHubClient
        from franktheunicorn.backends.poster import GitHubPoster

        token = getattr(settings, "FRANK_GITHUB_TOKEN", "")
        if not token:
            return HttpResponse(
                '<div class="recall-result" style="color: #c00;">Cannot recall: no token.</div>'
            )
        client = GitHubClient(token=token)
        try:
            poster = GitHubPoster(client)
            success = poster.recall_comment(draft)
        finally:
            client.close()
        if success:
            return render(request, "dashboard/_draft_item.html", {"draft": draft})
        return HttpResponse(
            '<div class="recall-result" style="color: #c00;">'
            "Recall failed (outside 24h window or API error).</div>"
        )
    except Exception:
        logger.exception("Failed to recall draft %d", draft.pk)
        return HttpResponse('<div class="recall-result" style="color: #c00;">Recall failed.</div>')


@require_POST
def post_review(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Post all approved findings for a PR as a single GitHub review."""
    pr = get_object_or_404(PullRequest, pk=pr_id)
    approved = list(
        ReviewDraft.objects.filter(pull_request=pr, status="accepted").order_by(
            "file_path", "line_number"
        )
    )

    if not approved:
        return HttpResponse('<div class="post-result">No approved findings to post.</div>')

    try:
        from django.conf import settings

        from franktheunicorn.backends.github import GitHubClient
        from franktheunicorn.backends.poster import GitHubPoster

        token = getattr(settings, "FRANK_GITHUB_TOKEN", "")
        if not token:
            return HttpResponse(
                '<div class="post-result" style="color: #c00;">'
                "Cannot post: GITHUB_TOKEN not configured.</div>"
            )

        client = GitHubClient(token=token)
        try:
            poster = GitHubPoster(client)
            poster.post_review(pr, approved)
        finally:
            client.close()

        return HttpResponse(
            f'<div class="post-result" style="color: #2e7d32;">'
            f"Posted {len(approved)} findings to GitHub.</div>"
        )
    except Exception:
        logger.exception("Failed to post review for PR #%d", pr.number)
        return HttpResponse(
            '<div class="post-result" style="color: #c00;">Failed to post review.</div>'
        )


def _is_agent_feedback_enabled() -> bool:
    """Check if direct agent feedback is enabled in operator config."""
    try:
        from django.conf import settings

        from franktheunicorn.config.loader import load_operator_config

        config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
        return config.agent_feedback.direct_session_enabled
    except Exception:
        return True  # default enabled per config schema


# --- Agent feedback (v1.25) ---


def compose_feedback(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Return HTML fragment with pre-populated feedback form for an AI-generated PR."""
    from franktheunicorn.review.feedback_formatter import format_feedback_markdown

    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)
    drafts = ReviewDraft.objects.filter(pull_request=pr).order_by("file_path", "line_number")
    test_runs = TestRun.objects.filter(pull_request=pr).order_by("-created_at")

    feedback_body = format_feedback_markdown(pr, drafts, test_runs, "needs-work")

    return render(
        request,
        "dashboard/_feedback_compose.html",
        {
            "pr": pr,
            "feedback_body": feedback_body,
        },
    )


@require_POST
def send_feedback(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Record agent feedback for a PR."""
    pr = get_object_or_404(PullRequest, pk=pr_id)
    assessment = request.POST.get("assessment", "needs-work")
    feedback_body = request.POST.get("feedback_body", "")

    valid_assessments = {choice[0] for choice in AgentFeedback.ASSESSMENT_CHOICES}
    if assessment not in valid_assessments:
        return HttpResponse(
            '<div class="feedback-result" style="color: #c00;">Invalid assessment value.</div>'
        )

    if not feedback_body.strip():
        return HttpResponse(
            '<div class="feedback-result" style="color: #c00;">Feedback body cannot be empty.</div>'
        )

    feedback_method = "session-url" if pr.agent_session_url else "github-comment"

    AgentFeedback.objects.create(
        pull_request=pr,
        assessment=assessment,
        feedback_body=feedback_body,
        feedback_method=feedback_method,
    )

    return render(request, "dashboard/_feedback_sent.html", {"pr": pr})


# --- Anti-pattern manager ---


def anti_pattern_list(request: HttpRequest) -> HttpResponse:
    """List all anti-patterns with filtering."""
    project_filter = request.GET.get("project")
    aps = AntiPattern.objects.all()
    if project_filter:
        aps = aps.filter(project__pk=project_filter)

    projects = Project.objects.filter(enabled=True).order_by("owner", "repo")
    return render(
        request,
        "dashboard/anti_patterns.html",
        {
            "anti_patterns": aps,
            "projects": projects,
            "active_project": project_filter,
        },
    )


@require_POST
def anti_pattern_create(request: HttpRequest) -> HttpResponse:
    """Create a new anti-pattern."""
    pattern_text = request.POST.get("pattern_text", "").strip()
    description = request.POST.get("description", "").strip()
    project_id = request.POST.get("project_id")

    if not pattern_text:
        return HttpResponse("Pattern text is required.", status=400)

    project = None
    if project_id:
        project = Project.objects.filter(pk=project_id).first()

    ap = AntiPattern.objects.create(
        pattern_text=pattern_text,
        description=description,
        project=project,
    )
    return render(request, "dashboard/_anti_pattern_row.html", {"ap": ap})


@require_POST
def anti_pattern_delete(request: HttpRequest, ap_id: int) -> HttpResponse:
    """Delete an anti-pattern."""
    ap = get_object_or_404(AntiPattern, pk=ap_id)
    ap.delete()
    return HttpResponse("")


@require_POST
def anti_pattern_toggle(request: HttpRequest, ap_id: int) -> HttpResponse:
    """Toggle an anti-pattern's is_active state."""
    ap = get_object_or_404(AntiPattern, pk=ap_id)
    ap.is_active = not ap.is_active
    ap.save(update_fields=["is_active", "updated_at"])
    return render(request, "dashboard/_anti_pattern_row.html", {"ap": ap})


# --- History & Stats ---


def stats(request: HttpRequest) -> HttpResponse:
    """History and stats view: review rates, costs, anti-pattern effectiveness."""
    actions = OperatorAction.objects.values("action_type").annotate(count=Count("id"))
    action_counts: dict[str, int] = {a["action_type"]: a["count"] for a in actions}

    total_cost = CostRecord.objects.aggregate(
        total=Sum("estimated_cost_usd"),
        total_tokens_in=Sum("tokens_in"),
        total_tokens_out=Sum("tokens_out"),
    )

    ap_stats = AntiPattern.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        total_triggers=Sum("times_triggered"),
    )

    total_drafts = ReviewDraft.objects.count()
    posted_drafts = ReviewDraft.objects.filter(status="posted").count()

    # Merge queue stats (v2).
    merge_eligible_count = PullRequest.objects.filter(
        state="open", merge_queue_eligible=True
    ).count()

    # Rejection predictor stats (v1.75).
    suppressed_count = ReviewDraft.objects.filter(is_auto_suppressed=True).count()
    scored_count = ReviewDraft.objects.filter(rejection_probability__isnull=False).count()

    # Shepherding stats (v2).
    shepherd_actions = OperatorAction.objects.filter(
        action_type__in=["accept_shepherd", "reject_shepherd", "edit_shepherd"],
    )
    shepherd_total = shepherd_actions.count()
    shepherd_rejected = shepherd_actions.filter(action_type="reject_shepherd").count()
    shepherd_rejection_rate = shepherd_rejected / shepherd_total if shepherd_total > 0 else 0.0

    return render(
        request,
        "dashboard/stats.html",
        {
            "action_counts": action_counts,
            "total_cost": total_cost.get("total") or Decimal("0"),
            "total_tokens_in": total_cost.get("total_tokens_in") or 0,
            "total_tokens_out": total_cost.get("total_tokens_out") or 0,
            "ap_stats": ap_stats,
            "total_drafts": total_drafts,
            "posted_drafts": posted_drafts,
            "suppressed_count": suppressed_count,
            "scored_count": scored_count,
            "shepherd_total": shepherd_total,
            "shepherd_rejected": shepherd_rejected,
            "shepherd_rejection_rate": shepherd_rejection_rate,
            "merge_eligible_count": merge_eligible_count,
        },
    )


# --- Merge Queue (v2) ---


def merge_queue_view(request: HttpRequest) -> HttpResponse:
    """Show PRs eligible for merging."""
    from django.conf import settings

    from franktheunicorn.config.loader import load_project_configs
    from franktheunicorn.worker.merge_queue import evaluate_merge_eligibility

    eligible_prs = (
        PullRequest.objects.filter(state="open", is_operator_pr=True)
        .select_related("project")
        .order_by("-interest_score")[:50]
    )

    # Load all project configs once and build a lookup dict.
    configs = load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", ""))
    config_by_project: dict[str, ProjectConfig] = {f"{c.owner}/{c.repo}": c for c in configs}

    pr_data: list[dict[str, object]] = []
    for pr in eligible_prs:
        # Load merge queue config for this project.
        try:
            pc = config_by_project.get(f"{pr.project.owner}/{pr.project.repo}")
            if pc and pc.merge_queue.enabled:
                eligibility = evaluate_merge_eligibility(pr, pc.merge_queue)
                merge_script = pc.merge_queue.merge_script
                merge_command = str(pr.number) if merge_script else ""
                pr_data.append(
                    {
                        "pr": pr,
                        "eligible": eligibility.eligible,
                        "ci_pass": eligibility.ci_pass,
                        "approvals_met": eligibility.approvals_met,
                        "no_conflicts": eligibility.no_conflicts,
                        "details": eligibility.details,
                        "merge_command": merge_command,
                    }
                )
        except Exception:
            logger.debug("Error loading merge config for %s", pr.project.full_name)

    return render(
        request,
        "dashboard/merge_queue.html",
        {"pr_data": pr_data},
    )


@require_POST
def merge_pr(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Execute a merge for a PR."""
    from django.conf import settings

    from franktheunicorn.backends.github import GitHubClient
    from franktheunicorn.config.loader import load_project_configs
    from franktheunicorn.worker.merge_queue import evaluate_merge_eligibility, execute_merge

    pr = get_object_or_404(PullRequest, pk=pr_id)

    configs = load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", ""))
    pc = next(
        (c for c in configs if c.owner == pr.project.owner and c.repo == pr.project.repo),
        None,
    )
    if not pc or not pc.merge_queue.enabled:
        return HttpResponse(
            '<div class="merge-result" style="color: #c00;">'
            "Merge queue not enabled for this project.</div>"
        )

    # Re-verify merge eligibility server-side before executing.
    eligibility = evaluate_merge_eligibility(pr, pc.merge_queue)
    if not eligibility.eligible:
        return HttpResponse(
            f'<div class="merge-result" style="color: #c00;">'
            f"PR is no longer eligible for merge: {eligibility.details}</div>"
        )

    token = getattr(settings, "FRANK_GITHUB_TOKEN", "")
    if not token:
        return HttpResponse(
            '<div class="merge-result" style="color: #c00;">'
            "Cannot merge: GITHUB_TOKEN not configured.</div>"
        )

    github_client = GitHubClient(token=token)
    try:
        result = execute_merge(pr, pc.merge_queue, github_client=github_client)
    finally:
        github_client.close()

    if result.success:
        return HttpResponse(
            f'<div class="merge-result" style="color: #2e7d32;">'
            f"Merged PR #{pr.number} via {result.method}.</div>"
        )
    return HttpResponse(
        f'<div class="merge-result" style="color: #c00;">Merge failed: {result.error}</div>'
    )


# --- Security Report Triage ---


SECURITY_STATUS_TABS: list[dict[str, str]] = [
    {"key": "all", "label": "All"},
    *[{"key": k, "label": v} for k, v in SecurityReport.STATUS_CHOICES],
]


def security_report_list(request: HttpRequest) -> HttpResponse:
    """List security reports with status tabs."""
    status_filter = request.GET.get("status", "all")
    reports = SecurityReport.objects.select_related("project").order_by("-created_at")

    if status_filter != "all":
        reports = reports.filter(status=status_filter)

    all_reports = SecurityReport.objects.all()
    all_count = all_reports.count()
    tabs_with_counts: list[dict[str, str | int]] = []
    for tab in SECURITY_STATUS_TABS:
        count = all_count if tab["key"] == "all" else all_reports.filter(status=tab["key"]).count()
        tabs_with_counts.append({**tab, "count": count})

    return render(
        request,
        "dashboard/security_list.html",
        {
            "reports": reports[:100],
            "status_tabs": tabs_with_counts,
            "active_status": status_filter,
        },
    )


def security_report_create(request: HttpRequest) -> HttpResponse:
    """Paste form for creating a new security report."""
    if request.method == "POST":
        raw_text = request.POST.get("raw_text", "").strip()
        title = request.POST.get("title", "").strip()
        project_id = request.POST.get("project_id")
        reporter_name = request.POST.get("reporter_name", "").strip()
        reporter_email = request.POST.get("reporter_email", "").strip()

        if not raw_text:
            return HttpResponse("Report text is required.", status=400)

        project = None
        if project_id:
            project = Project.objects.filter(pk=project_id).first()

        report = SecurityReport.objects.create(
            raw_text=raw_text,
            title=title,
            project=project,
            reporter_name=reporter_name,
            reporter_email=reporter_email,
            source="paste",
        )

        # Auto-triage if configured.
        try:
            _auto_triage_report(report)
        except Exception:
            logger.debug("Auto-triage failed for report %d", report.pk, exc_info=True)

        return redirect("dashboard:security_detail", report_id=report.pk)

    projects = Project.objects.filter(enabled=True).order_by("owner", "repo")
    return render(
        request,
        "dashboard/security_create.html",
        {"projects": projects},
    )


def security_report_detail(request: HttpRequest, report_id: int) -> HttpResponse:
    """Detail view for a single security report."""
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    sandbox_enabled = _is_sandbox_enabled()

    return render(
        request,
        "dashboard/security_detail.html",
        {
            "report": report,
            "sandbox_enabled": sandbox_enabled,
        },
    )


@require_POST
def security_report_triage(request: HttpRequest, report_id: int) -> HttpResponse:
    """Trigger LLM triage on a security report (htmx)."""
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    try:
        from franktheunicorn.config.loader import get_operator_config

        operator_config = get_operator_config()

        if not operator_config.llm_backends:
            return HttpResponse(
                '<div class="triage-result" style="color: #c00;">'
                "No LLM backend configured. Add one to operator.yaml.</div>"
            )

        project_config = _find_project_config(report.project) if report.project else None

        from franktheunicorn.security.triage import triage_report

        triage_report(report, project_config, operator_config)
        report.refresh_from_db()
    except Exception:
        logger.exception("Triage failed for report %d", report.pk)
        return HttpResponse(
            '<div class="triage-result" style="color: #c00;">'
            "Triage failed. Check LLM backend configuration.</div>"
        )

    return render(request, "dashboard/_security_triage_result.html", {"report": report})


@require_POST
def security_report_verdict(request: HttpRequest, report_id: int) -> HttpResponse:
    """Set operator verdict on a security report (htmx)."""
    report = get_object_or_404(SecurityReport, pk=report_id)
    new_status = request.POST.get("status", "")
    notes = request.POST.get("operator_notes", "")

    valid_statuses = {choice[0] for choice in SecurityReport.STATUS_CHOICES}
    if new_status not in valid_statuses:
        return HttpResponse("Invalid status.", status=400)

    report.status = new_status
    report.operator_notes = notes
    if new_status == "duplicate":
        report.matched_cve_id = request.POST.get("matched_cve_id", "")
    else:
        report.matched_cve_id = ""
    report.save(update_fields=["status", "operator_notes", "matched_cve_id", "updated_at"])

    return render(request, "dashboard/_security_verdict.html", {"report": report})


@require_POST
def security_report_sandbox(request: HttpRequest, report_id: int) -> HttpResponse:
    """Trigger sandbox POC execution (htmx)."""
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    if not _is_sandbox_enabled():
        return HttpResponse(
            '<div class="sandbox-result" style="color: #c00;">'
            "Sandbox execution is not enabled.</div>"
        )

    try:
        from franktheunicorn.security.sandbox import run_poc_in_sandbox

        repo_path = None
        if report.project:
            from django.conf import settings

            repos_dir = getattr(settings, "FRANK_REPOS_DIR", "")
            if repos_dir:
                from pathlib import Path

                candidate = Path(repos_dir) / report.project.owner / report.project.repo
                if candidate.is_dir():
                    repo_path = candidate

        result = run_poc_in_sandbox(report, repo_path=repo_path)
        report.sandbox_requested = True
        report.sandbox_verdict = result.verdict
        report.sandbox_result = result.output
        report.save(
            update_fields=[
                "sandbox_requested",
                "sandbox_verdict",
                "sandbox_result",
                "updated_at",
            ]
        )
    except Exception:
        logger.exception("Sandbox execution failed for report %d", report.pk)
        return HttpResponse(
            '<div class="sandbox-result" style="color: #c00;">Sandbox execution failed.</div>'
        )

    return render(request, "dashboard/_security_sandbox_result.html", {"report": report})


@require_POST
def security_report_cve_check(request: HttpRequest, report_id: int) -> HttpResponse:
    """Trigger CVE lookup (htmx)."""
    report = get_object_or_404(SecurityReport, pk=report_id)

    try:
        from franktheunicorn.config.loader import get_operator_config

        operator_config = get_operator_config()

        if not operator_config.security_triage.enabled:
            return HttpResponse(
                '<div class="cve-result" style="color: #c00;">'
                "Security triage is not enabled in operator config.</div>"
            )

        keyword = report.parsed_component or report.title
        if not keyword:
            return HttpResponse('<div class="cve-result">No component or title to search.</div>')

        from franktheunicorn.security.cve_lookup import search_cves

        api_key_env = operator_config.security_triage.nvd_api_key_env
        matches = search_cves(keyword, api_key_env=api_key_env)
        report.cve_matches = [m.to_dict() for m in matches]
        report.save(update_fields=["cve_matches", "updated_at"])
    except Exception:
        logger.exception("CVE check failed for report %d", report.pk)
        return HttpResponse('<div class="cve-result" style="color: #c00;">CVE lookup failed.</div>')

    return render(request, "dashboard/_security_cve_matches.html", {"report": report})


def _auto_triage_report(report: SecurityReport) -> None:
    """Auto-triage a new report if configured."""
    from franktheunicorn.config.loader import get_operator_config

    operator_config = get_operator_config()
    if not operator_config.security_triage.enabled:
        return
    if not operator_config.security_triage.auto_triage:
        return

    project_config = _find_project_config(report.project) if report.project else None

    from franktheunicorn.security.triage import triage_report

    triage_report(report, project_config, operator_config)


def _is_sandbox_enabled() -> bool:
    """Check if sandbox execution is enabled in operator config."""
    try:
        from franktheunicorn.config.loader import get_operator_config

        return get_operator_config().security_triage.sandbox_enabled
    except Exception:
        return False


def _find_project_config(project: Project) -> ProjectConfig | None:
    """Look up the ProjectConfig YAML for a given Project model instance."""
    from django.conf import settings

    from franktheunicorn.config.loader import load_project_configs

    configs = load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", ""))
    return next(
        (c for c in configs if c.owner == project.owner and c.repo == project.repo),
        None,
    )


def _ingest_single_pr(owner: str, repo: str, pr_number: int) -> PullRequest:
    from franktheunicorn.backends.poller import ingest_single_pr

    return ingest_single_pr(owner, repo, pr_number)


@require_POST
def run_agents(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Trigger on-demand LLM agent review for a PR (htmx)."""
    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)
    try:
        from franktheunicorn.config.loader import get_operator_config, get_project_config
        from franktheunicorn.worker.runner import process_pr

        operator_config = get_operator_config()
        project_config = get_project_config(pr.project.full_name)
        if not project_config:
            return HttpResponse(
                '<div class="run-agents-result" style="color: #c00;">'
                "No project config found for this repo.</div>"
            )
        log_lines: list[str] = []
        drafts = process_pr(pr, project_config, operator_config, force=True, log_lines=log_lines)
        log_html = "".join(f"<li>{line}</li>" for line in log_lines)
        return HttpResponse(
            f'<div class="run-agents-result">'
            f'<p style="color: #2e7d32; margin: 0 0 0.5rem;">'
            f"Generated {len(drafts)} finding(s). Reload to see updated results.</p>"
            f'<ul class="agent-log">{log_html}</ul>'
            f"</div>"
        )
    except Exception:
        logger.exception("Failed to run agents for PR #%d", pr.pk)
        return HttpResponse(
            '<div class="run-agents-result" style="color: #c00;">Agent run failed. Check server logs.</div>'
        )


@require_POST
def run_dual_tests(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Trigger a manual differential test run for a PR (htmx).

    Runs in a background thread so the HTTP response returns immediately.
    The operator can reload the PR detail page to see the result once complete.
    The trusted-author gate is bypassed because the operator explicitly requested
    the run.
    """
    from pathlib import Path

    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)

    try:
        from django.conf import settings

        from franktheunicorn.config.loader import get_project_config
        from franktheunicorn.worker.test_runner import TestRunner

        project_config = get_project_config(pr.project.full_name)
        if not project_config:
            return HttpResponse(
                '<div class="run-tests-result" style="color: #c00;">'
                "No project config found for this repo.</div>"
            )
        if not project_config.tests.enabled:
            return HttpResponse(
                '<div class="run-tests-result" style="color: #c00;">'
                "Differential tests are not enabled for this project. "
                "Add <code>tests: enabled: true</code> to the project YAML.</div>"
            )

        repos_dir = getattr(settings, "FRANK_REPOS_DIR", "")
        repo_path: Path | None = None
        if repos_dir:
            candidate = Path(repos_dir) / pr.project.owner / pr.project.repo
            if candidate.is_dir():
                repo_path = candidate

        runner = TestRunner()

        def _run() -> None:
            runner.run_differential_test(pr, project_config, repo_path=repo_path, force=True)

        thread = threading.Thread(target=_run, daemon=True, name=f"dual-test-pr-{pr.pk}")
        thread.start()

    except Exception:
        logger.exception("Failed to start dual test run for PR #%d", pr.pk)
        return HttpResponse(
            '<div class="run-tests-result" style="color: #c00;">Failed to start test run. Check server logs.</div>'
        )

    return HttpResponse(
        '<div class="run-tests-result" style="color: #2e7d32;">'
        "Test run started. Reload this page in a few minutes to see the verdict.</div>"
    )


def _resolve_and_redirect_pr(
    request: HttpRequest, owner: str, repo: str, pr_number: int
) -> HttpResponse:
    """Look up a PR in the DB; ingest on-demand from the forge if absent.

    Redirects to pr_detail on success, or to index with an error message on failure.
    """
    try:
        pr = PullRequest.objects.select_related("project").get(
            project__owner=owner, project__repo=repo, number=pr_number
        )
        return redirect("dashboard:pr_detail", pr_id=pr.pk)
    except PullRequest.DoesNotExist:
        pass

    try:
        pr = _ingest_single_pr(owner, repo, pr_number)
        return redirect("dashboard:pr_detail", pr_id=pr.pk)
    except Exception as exc:
        logger.warning("On-demand ingest failed for %s/%s#%d: %s", owner, repo, pr_number, exc)
        messages.error(request, f"Could not fetch PR #{pr_number} from {owner}/{repo}.")
        return redirect("dashboard:index")


def lookup_pr(request: HttpRequest) -> HttpResponse:
    """Look up a PR by project + number; ingest on-demand if not yet in the DB."""
    if request.method != "POST":
        return redirect("dashboard:index")

    project_str = request.POST.get("project", "").strip()
    raw_number = request.POST.get("pr_number", "").strip()

    if "/" not in project_str or not raw_number.isdigit():
        messages.error(request, "Enter a valid project and PR number.")
        return redirect("dashboard:index")

    owner, repo = project_str.split("/", 1)
    return _resolve_and_redirect_pr(request, owner, repo, int(raw_number))


def pr_by_coords(request: HttpRequest, owner: str, repo: str, pr_number: int) -> HttpResponse:
    """Resolve a PR by owner/repo/number via a bookmarkable GET URL.

    Redirects to pr_detail if already in the DB; ingests on-demand otherwise.
    Useful for deep-linking directly to a PR from external tools or browser bookmarks.
    """
    return _resolve_and_redirect_pr(request, owner, repo, pr_number)
