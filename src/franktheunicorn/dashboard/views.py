"""
Dashboard views — server-rendered HTML with htmx interactivity.

Function-based views. No SPA, no React. htmx for all dynamic updates.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from franktheunicorn.core.models import (
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

    return render(request, "dashboard/pr_list.html", {
        "pull_requests": prs,
        "queue_tabs": QUEUE_TABS,
        "active_queue": queue,
        "queue_counts": queue_counts,
        "workspace": workspace,
        "workspaces": workspaces,
    })


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
                desc = val.get("description", key) if isinstance(val, dict) else key
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
    drafts = ReviewDraft.objects.filter(pull_request=pr).order_by("file_path", "line_number")
    dep_changes = DependencyChange.objects.filter(pull_request=pr).order_by("package_name")
    test_runs = TestRun.objects.filter(pull_request=pr).order_by("-created_at")
    return render(
        request,
        "dashboard/pr_detail.html",
        {
            "pr": pr,
            "drafts": drafts,
            "dep_changes": dep_changes,
            "test_runs": test_runs,
        },
    )


# --- Finding actions (htmx) ---


@require_POST
def approve_draft(request: HttpRequest, draft_id: int) -> HttpResponse:
    """Approve a draft finding."""
    draft = get_object_or_404(ReviewDraft, pk=draft_id)
    draft.status = "accepted"
    draft.save(update_fields=["status", "updated_at"])

    OperatorAction.objects.create(
        action_type="accept_draft",
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
        action_type="reject_draft",
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
            action_type="edit_draft",
            review_draft=draft,
            pull_request=draft.pull_request,
        )
    return render(request, "dashboard/_draft_item.html", {"draft": draft})


@require_POST
def post_review(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Post all approved findings for a PR as a single GitHub review."""
    pr = get_object_or_404(PullRequest, pk=pr_id)
    approved = list(
        ReviewDraft.objects.filter(
            pull_request=pr, status="accepted"
        ).order_by("file_path", "line_number")
    )

    if not approved:
        return HttpResponse(
            '<div class="post-result">No approved findings to post.</div>'
        )

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


# --- Anti-pattern manager ---


def anti_pattern_list(request: HttpRequest) -> HttpResponse:
    """List all anti-patterns with filtering."""
    project_filter = request.GET.get("project")
    aps = AntiPattern.objects.all()
    if project_filter:
        aps = aps.filter(project__pk=project_filter)

    projects = Project.objects.filter(enabled=True).order_by("owner", "repo")
    return render(request, "dashboard/anti_patterns.html", {
        "anti_patterns": aps,
        "projects": projects,
        "active_project": project_filter,
    })


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

    return render(request, "dashboard/stats.html", {
        "action_counts": action_counts,
        "total_cost": total_cost.get("total") or Decimal("0"),
        "total_tokens_in": total_cost.get("total_tokens_in") or 0,
        "total_tokens_out": total_cost.get("total_tokens_out") or 0,
        "ap_stats": ap_stats,
        "total_drafts": total_drafts,
        "posted_drafts": posted_drafts,
    })
