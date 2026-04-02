"""
Dashboard views — server-rendered HTML with htmx interactivity.

Function-based views. No SPA, no React. htmx for all dynamic updates.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from franktheunicorn.core.models import (
    AgentFeedback,
    AntiPattern,
    CostRecord,
    DependencyChange,
    OperatorAction,
    Project,
    PullRequest,
    ReviewDraft,
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
]


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


def index(request: HttpRequest) -> HttpResponse:
    """Main dashboard: list of PRs sorted by interest score with queue tabs."""
    queue = request.GET.get("queue", "review")
    workspace_projects = _get_workspace_projects(request)

    prs = (
        PullRequest.objects.select_related("project")
        .filter(state="open", queue=queue)
        .order_by("-interest_score", "-github_updated_at")
    )

    if workspace_projects is not None:
        project_filters = Q()
        for full_name in workspace_projects:
            parts = full_name.split("/", 1)
            if len(parts) == 2:
                project_filters |= Q(project__owner=parts[0], project__repo=parts[1])
        prs = prs.filter(project_filters)

    prs = prs[:100]

    # Count PRs per queue for tab badges.
    queue_counts: dict[str, int] = {}
    base_qs = PullRequest.objects.filter(state="open")
    for tab in QUEUE_TABS:
        queue_counts[tab["key"]] = base_qs.filter(queue=tab["key"]).count()

    workspace = request.COOKIES.get("workspace", "all")
    workspaces = _get_workspace_list()

    return render(
        request,
        "dashboard/pr_list.html",
        {
            "pull_requests": prs,
            "queue_tabs": QUEUE_TABS,
            "active_queue": queue,
            "queue_counts": queue_counts,
            "workspace": workspace,
            "workspaces": workspaces,
        },
    )


def _get_workspace_list() -> list[dict[str, str]]:
    """Get available workspaces from config."""
    workspaces = [{"key": "all", "label": "All Projects"}]
    try:
        from django.conf import settings

        from franktheunicorn.config.loader import load_operator_config

        config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
        raw = getattr(config, "workspaces", {})
        if raw and isinstance(raw, dict):
            for key, val in raw.items():
                desc = str(val.get("description", key)) if isinstance(val, dict) else str(key)
                workspaces.append({"key": key, "label": desc})
    except Exception:
        pass
    return workspaces


def set_workspace(request: HttpRequest) -> HttpResponse:
    """Set the active workspace via cookie."""
    workspace = request.POST.get("workspace", "all")
    response = redirect("dashboard:index")
    response.set_cookie("workspace", workspace, max_age=86400 * 365)
    return response


def pr_detail(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Detail view for a single PR showing drafts and score breakdown."""
    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)
    drafts = ReviewDraft.objects.filter(
        pull_request=pr,
        is_auto_suppressed=False,
    ).order_by("file_path", "line_number")
    suppressed_drafts = ReviewDraft.objects.filter(
        pull_request=pr,
        is_auto_suppressed=True,
    ).order_by("file_path", "line_number")
    dep_changes = DependencyChange.objects.filter(pull_request=pr).order_by("package_name")
    test_runs = TestRun.objects.filter(pull_request=pr).order_by("-created_at")

    # Check if agent feedback is enabled (v1.25).
    feedback_enabled = _is_agent_feedback_enabled()

    # Load personality name for template display.
    from franktheunicorn.config.loader import get_operator_config

    personality_name = get_operator_config().personality

    # v1.5: Separate CodeRabbit-sourced findings.
    coderabbit_drafts = [d for d in drafts if any("coderabbit" in s for s in (d.sources or []))]
    agent_drafts = [d for d in drafts if not any("coderabbit" in s for s in (d.sources or []))]

    # v1.5: External context (JIRA, community, Sentry).
    jira_context = pr.jira_cache if pr.jira_cache else None
    community_context = pr.community_context_cache if pr.community_context_cache else None
    sentry_context = pr.sentry_context_cache if pr.sentry_context_cache else None

    return render(
        request,
        "dashboard/pr_detail.html",
        {
            "pr": pr,
            "drafts": drafts,
            "suppressed_drafts": suppressed_drafts,
            "agent_drafts": agent_drafts,
            "coderabbit_drafts": coderabbit_drafts,
            "dep_changes": dep_changes,
            "test_runs": test_runs,
            "feedback_enabled": feedback_enabled,
            "personality_name": personality_name,
            "jira_context": jira_context,
            "community_context": community_context,
            "sentry_context": sentry_context,
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
    if draft.status != "posted" or not draft.github_comment_id:
        return HttpResponse(
            '<div class="recall-result" style="color: #c00;">Cannot recall: not posted.</div>'
        )
    try:
        from django.conf import settings

        from franktheunicorn.github.client import GitHubClient
        from franktheunicorn.github.poster import GitHubPoster

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

        from franktheunicorn.github.client import GitHubClient
        from franktheunicorn.github.poster import GitHubPoster

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
    from franktheunicorn.config.models import ProjectConfig
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
                merge_command = (
                    f"{merge_script} {pr.number} {pr.project.full_name}" if merge_script else ""
                )
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

    from franktheunicorn.config.loader import load_project_configs
    from franktheunicorn.github.client import GitHubClient
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
