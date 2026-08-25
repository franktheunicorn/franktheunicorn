"""
Dashboard views — server-rendered HTML with htmx interactivity.

Function-based views. No SPA, no React. htmx for all dynamic updates.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.security.zip_import import ZipImportResult

from django.contrib import messages
from django.db.models import Count, Max, Q, Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from franktheunicorn.core.models import (
    AgentFeedback,
    AgentVibe,
    AntiPattern,
    CostRecord,
    DependencyChange,
    EmailScanRecord,
    OperatorAction,
    Project,
    PullRequest,
    ReviewDraft,
    SecurityReport,
    SecurityTriageGuidance,
    TestRun,
    WorkerCommand,
)

logger = logging.getLogger(__name__)

# Queue definitions for the tab bar.
QUEUE_TABS: list[dict[str, str]] = [
    {"key": "review", "label": "Review"},
    {"key": "your-prs", "label": "Your PRs"},
    {"key": "mentioned", "label": "Mentioned"},
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
        .annotate(last_local_action_at=Max("actions__created_at"))
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


@require_POST
def set_workspace(request: HttpRequest) -> HttpResponse:
    """Set the active workspace via cookie.

    POST-only because this is a mutation. The cookie is HttpOnly + SameSite=Lax
    — it stores a workspace identifier, not a session token, but JS access is
    unnecessary and SameSite limits CSRF surface.
    """
    from django.conf import settings

    workspace = request.POST.get("workspace", "all")
    response = redirect("dashboard:index")
    response.set_cookie(
        "workspace",
        workspace,
        max_age=86400 * 365,
        httponly=True,
        samesite="Lax",
        secure=not settings.DEBUG,
    )
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
    pr = get_object_or_404(
        PullRequest.objects.select_related("project").annotate(
            last_local_action_at=Max("actions__created_at"),
        ),
        pk=pr_id,
    )
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

    # v1.5: External context (JIRA).
    jira_context = pr.jira_cache if pr.jira_cache else None
    jira_server = (
        project_config.jira.server if project_config and project_config.jira.server else ""
    )

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
            "agent_vibes": agent_vibes,
            "dep_changes": dep_changes,
            "test_runs": test_runs,
            "feedback_enabled": feedback_enabled,
            "personality_name": personality_name,
            "jira_context": jira_context,
            "jira_server": jira_server,
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


def _make_posting_client(pr: PullRequest) -> object | None:
    """Build a forge client for the PR's project (GitHub, Gitea, GitLab).

    Mirrors ``poller.ingest_single_pr``'s resolution: the project's YAML
    ``forge`` picks the registry entry. Hardcoding GitHub here posted
    reviews for Gitea/GitLab projects to api.github.com — at best a 404, at
    worst a same-named public GitHub repo. Falls back to a plain GitHub
    client when config resolution fails (e.g. tests with no YAML on disk).
    """
    try:
        from franktheunicorn.backends import make_client
        from franktheunicorn.config.loader import get_operator_config, get_project_config
        from franktheunicorn.config.resolver import get_forge_entry

        operator_config = get_operator_config()
        project_config = get_project_config(pr.project.full_name)
        forge_name = getattr(project_config, "forge", None) or "github"
        entry = get_forge_entry(operator_config, forge_name)
        # A view: track quota, but never block the operator's click on it.
        return make_client(entry, pace_requests=False)
    except Exception:
        logger.debug(
            "Forge resolution failed for %s; falling back to GitHub client.",
            pr.project.full_name,
            exc_info=True,
        )
        from django.conf import settings

        from franktheunicorn.backends.github import GitHubClient

        token = getattr(settings, "FRANK_GITHUB_TOKEN", "")
        if not token:
            return None
        return GitHubClient(token=token)


@require_POST
def recall_draft(request: HttpRequest, draft_id: int) -> HttpResponse:
    """Recall (delete) a posted comment from the forge within the recall window."""
    draft = get_object_or_404(
        ReviewDraft.objects.select_related("pull_request__project"), pk=draft_id
    )

    def _item_with_error(message: str) -> HttpResponse:
        # Re-render the full draft item — returning a bare error div would
        # replace (and erase) the finding via the outerHTML swap.
        return render(
            request,
            "dashboard/_draft_item.html",
            {"draft": draft, "recall_error": message},
        )

    if draft.status != "posted" or not draft.forge_comment_id:
        return _item_with_error("Cannot recall: not posted.")
    try:
        from franktheunicorn.backends.poster import GitHubPoster

        client = _make_posting_client(draft.pull_request)
        if client is None:
            return _item_with_error("Cannot recall: no forge client/token configured.")
        try:
            poster = GitHubPoster(client)  # type: ignore[arg-type]
            success = poster.recall_comment(draft)
        finally:
            client.close()  # type: ignore[attr-defined]
        if success:
            return render(request, "dashboard/_draft_item.html", {"draft": draft})
        return _item_with_error("Recall failed (outside 24h window or API error).")
    except Exception:
        logger.exception("Failed to recall draft %d", draft.pk)
        return _item_with_error("Recall failed.")


@require_POST
def post_review(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Post approved and edited findings for a PR as a single forge review."""
    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)
    # Edited drafts are included: the operator's rewrite is the strongest
    # approval signal, and the poster prefers edited_body when present.
    approved = list(
        ReviewDraft.objects.filter(pull_request=pr, status__in=["accepted", "edited"]).order_by(
            "file_path", "line_number"
        )
    )

    if not approved:
        return HttpResponse('<div class="post-result">No approved findings to post.</div>')

    try:
        from franktheunicorn.backends.poster import GitHubPoster

        client = _make_posting_client(pr)
        if client is None:
            return HttpResponse(
                '<div class="post-result" style="color: #c00;">'
                "Cannot post: no forge client/token configured.</div>"
            )

        try:
            poster = GitHubPoster(client)  # type: ignore[arg-type]
            poster.post_review(pr, approved)
        finally:
            client.close()  # type: ignore[attr-defined]

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
    # Ignore malformed filters (?project=abc would 500 in the pk lookup),
    # matching the index view's treatment of bad query params.
    if project_filter and not project_filter.isdigit():
        project_filter = None
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

    # Rejection predictor stats (v1.75).
    suppressed_count = ReviewDraft.objects.filter(is_auto_suppressed=True).count()
    scored_count = ReviewDraft.objects.filter(rejection_probability__isnull=False).count()

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
            "projects": Project.objects.filter(enabled=True).order_by("owner", "repo"),
            "zip_import_command": _zip_import_command(),
        },
    )


def _zip_import_command() -> str:
    """The shell command for importing a report archive on *this* install.

    Printed on the security page beside the upload button, for an archive too
    big to push through a browser or a box where the dashboard isn't reachable.

    Has to branch on containerisation, and getting this wrong is worse than
    printing nothing: under ``docker compose`` this process is
    ``/usr/local/bin/python`` at ``/app``, and an operator reading that off the
    dashboard would paste it into a *host* shell that has neither the
    interpreter, the code, nor the archive path. The compose form also routes
    the archive through ``./data``, which is the bind mount both containers
    share — so the file the operator drops next to the DB is the file the
    command can actually open.
    """
    import sys
    from pathlib import Path

    from django.conf import settings

    if _running_in_container():
        # Both forms: the image can tell us it's a container, not which
        # orchestrator, and the same image ships in compose.yaml and k8s/deploy.yaml.
        # Renders on two lines inside the <pre>.
        return (
            "docker compose exec web python manage.py import_security_zip data/reports.zip\n"
            "kubectl exec deploy/franktheunicorn-web -- "
            "python manage.py import_security_zip data/reports.zip"
        )

    base = Path(settings.BASE_DIR)
    interpreter = "python"
    if sys.executable:
        exe = Path(sys.executable)
        # Repo-relative when it's the venv make setup leaves behind, so the line
        # can be pasted from the repo root as-is.
        interpreter = str(exe.relative_to(base)) if exe.is_relative_to(base) else str(exe)

    return f"{interpreter} manage.py import_security_zip reports.zip"


def _running_in_container() -> bool:
    """Whether this process is inside the shipped image.

    ``FRANK_IN_CONTAINER`` comes from the Dockerfile, which is the only party
    that actually knows. Sniffing the runtime instead got k8s wrong: the manifest
    runs this same image under containerd, which creates no ``/.dockerenv``, so
    the dashboard printed the host-shell command its own caller calls worse than
    printing nothing.

    ``/.dockerenv`` stays as a fallback for an image built before the env var, and
    "not a container" is the safe default — the venv form at least names a real
    interpreter.
    """
    import os
    from pathlib import Path

    if os.environ.get("FRANK_IN_CONTAINER", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        return Path("/.dockerenv").exists()
    except OSError:  # pragma: no cover - unreadable root
        return False


def email_activity(request: HttpRequest) -> HttpResponse:
    """Read-only audit of every email the security scanner has examined.

    Makes the "is the tool reading my mail?" question answerable at a glance:
    one row per message the read-only IMAP scanner opened, showing who it was
    from, whether it was a forward, which security keywords matched, and
    whether it became a report. The scanner never marks mail seen and never
    sends anything.
    """
    records = EmailScanRecord.objects.select_related("security_report").all()

    email_configured = False
    try:
        from django.conf import settings

        from franktheunicorn.config.loader import load_operator_config

        cfg = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
        email_configured = bool(cfg.security_triage.enabled and cfg.security_triage.email.enabled)
    except Exception:
        logger.debug("Could not load operator config for email activity view", exc_info=True)

    counts = {
        "examined": records.count(),
        "ingested": records.filter(action="ingested").count(),
        "skipped_not_security": records.filter(action="skipped_not_security").count(),
        "skipped_duplicate": records.filter(action="skipped_duplicate").count(),
    }

    return render(
        request,
        "dashboard/email_activity.html",
        {
            "records": records[:200],
            "counts": counts,
            "email_configured": email_configured,
        },
    )


#: Cap on an uploaded archive, checked before the importer touches it.
#:
#: Not an ingress limit, and there is no other one: Django has already received
#: the whole body by the time a view runs, ``DATA_UPLOAD_MAX_MEMORY_SIZE``
#: deliberately excludes file fields, and the shipped compose file publishes
#: gunicorn directly with no proxy in front of it — so a multi-GB POST is spooled
#: into the container's temp dir, next to the SQLite file both services share,
#: before this check ever runs. Fixing that properly needs a body limit at the
#: server or a proxy; what this does is keep the number small enough that the
#: spool is survivable. 2000 text reports is a few MB, so a tight cap costs
#: nothing real — the earlier 64 MB bounded nothing that mattered while leaving
#: room for a 650k-entry central directory that costs ~380 MB just to reject.
MAX_SECURITY_ZIP_UPLOAD_BYTES = 8 * 1024 * 1024

#: Entry cap for the *web* path, well below the importer's own MAX_ENTRIES.
#:
#: The whole import — parse, insert and triage-enqueue per entry — runs inside
#: this HTTP request, against the SQLite file the worker is also writing. That
#: makes this endpoint a third violation of the "no long work in the web
#: container" rule CLAUDE.md sets out, alongside run_dual_tests and
#: security_report_sandbox; the real fix is an ``import_security_zip``
#: WorkerCommand (migration 0030 added run_security_triage the same way, and
#: compose already mounts ./data on both services so the worker can read a staged
#: file). Until that lands, this bounds a request to a couple of hundred inserts
#: and points anything larger at the CLI, which has no such problem.
#:
#: Sizing note: every deployment now runs gthread with ``--timeout 90`` —
#: compose.yaml, k8s/deploy.yaml and scripts/run_local_all.sh alike. (This
#: comment used to justify 200 from local ``make up`` running the *sync* worker
#: on gunicorn's default 30s; that stopped being true in the same push, so the
#: number is bounded by insert volume and lock contention rather than by the
#: reaper.) 200 entries measured ~0.5s of SQLite commits, and the risk is the
#: worker's write lock, not the clock — ``settings.py`` gives each commit a 20s
#: busy timeout, so the cap is about how many chances to block, not elapsed time.
MAX_SYNCHRONOUS_ZIP_ENTRIES = 200


@require_POST
def security_report_upload(request: HttpRequest) -> HttpResponse:
    """Import a zip of security reports uploaded through the dashboard.

    The same importer the ``import_security_zip`` command uses, so a backlog can
    be dragged into the browser instead of scp'd to wherever the worker runs.
    Reports land as drafts for triage exactly like a pasted one — nothing is
    posted or sent as a result of an upload.
    """
    from franktheunicorn.security.zip_import import import_reports_from_zip

    upload = request.FILES.get("zip_file")
    if upload is None:
        messages.error(request, "Choose a .zip file to import.")
        return redirect("dashboard:security_list")

    if upload.size and upload.size > MAX_SECURITY_ZIP_UPLOAD_BYTES:
        limit_mb = MAX_SECURITY_ZIP_UPLOAD_BYTES // (1024 * 1024)
        messages.error(request, f"That archive is larger than the {limit_mb} MB upload limit.")
        return redirect("dashboard:security_list")

    project = None
    project_id = request.POST.get("project_id")
    if project_id:
        # filter(pk=...) on a non-numeric value raises ValueError, which is an
        # unhandled 500 on an endpoint anyone on the Tailscale net can POST to.
        # security_report_create guards the same field this way.
        try:
            project = Project.objects.filter(pk=int(project_id)).first()
        except (TypeError, ValueError):
            messages.error(request, "That project selection wasn't valid.")
            return redirect("dashboard:security_list")

    # Off unless the operator ticked the box. A backlog import fans out to an
    # NVD lookup and two LLM calls per report, which is not a thing to start by
    # accident from a file picker.
    auto_triage = request.POST.get("auto_triage") == "on"

    # Not gated on the file name: the importer decides by content and reports a
    # non-zip as an error, so a ".ZIP" or an extensionless export still works.
    result = import_reports_from_zip(
        upload,
        project=project,
        auto_triage=auto_triage,
        max_entries=MAX_SYNCHRONOUS_ZIP_ENTRIES,
    )

    if result.error and not result.imported:
        messages.error(request, f"Import failed: {result.error}")
        if result.over_entry_cap:
            messages.info(
                request,
                "Archives that big are better done from a shell — the browser path "
                "runs the whole import inside one request. See the command on this page.",
            )
    elif result.imported:
        # summary() names result.error too when a cap tripped part-way, so a
        # partial import reads as "N imported, then stopped: …" rather than
        # either a clean success or a bare failure.
        level = messages.warning if result.error else messages.success
        level(request, f"Imported from {upload.name}: {result.summary()}")
        if not result.queued_triage:
            # The reason comes from the importer, never inferred from
            # queued_triage == 0 — that sent operators to check a setting that was
            # already correct when the real cause was a bad config or an
            # already-in-flight run.
            if result.triage_skipped_reason:
                reason = f"{result.triage_skipped_reason}."
            elif auto_triage:
                reason = "no triage runs were queued — check the worker log."
            else:
                reason = "open one and hit Run LLM Triage, or re-upload with triage ticked."
            messages.info(request, f"{result.imported} report(s) imported untriaged — {reason}")
    elif result.duplicates and not result.failed:
        # The hint above the button advertises re-import as safe, so the case it
        # invites must not come back as a warning-coloured "nothing imported".
        messages.success(
            request,
            f"{upload.name}: already imported — "
            f"all {result.duplicates} report(s) were already present.",
        )
    else:
        messages.warning(request, f"Nothing imported from {upload.name}: {result.summary()}")

    _report_failed_entries(request, result)
    return redirect("dashboard:security_list")


#: How many individual entry failures to name before summarising the rest.
#: FallbackStorage spills past the cookie into the session rather than dropping
#: them, so this is about legibility, not loss: one message per bad entry in a
#: thousand-entry archive is a page of identical paragraphs the operator has to
#: scroll past to find the part that worked.
MAX_UPLOAD_ENTRY_MESSAGES = 8


#: Outcomes worth naming: everything that isn't a report the operator now has.
#:
#: "error" and "too-large" alone wasn't enough. A dropped entry is dropped
#: whether the importer calls it a failure or a decision, and the ones it calls
#: decisions are the ones most likely to be wrong — a real report that trips only
#: one security keyword reads as "not-a-report", and a 7-Zip archive is
#: "unsupported" for every entry, so an operator saw "0 imported, 47 skipped"
#: with no filename and no reason. There is no --no-filter or --verbose-entries
#: on this door to go looking with.
_REPORTED_ENTRY_OUTCOMES = ("error", "too-large", "not-a-report", "unsupported", "empty")


def _report_failed_entries(request: HttpRequest, result: ZipImportResult) -> None:
    """Name the first few entries that didn't import, then count the rest.

    Not "duplicate" — that one the operator already has, and the re-import hint
    advertises it as the expected case.
    """
    missed = [entry for entry in result.entries if entry.outcome in _REPORTED_ENTRY_OUTCOMES]
    for entry in missed[:MAX_UPLOAD_ENTRY_MESSAGES]:
        detail = f" — {entry.detail}" if entry.detail else ""
        messages.warning(request, f"{entry.name}: {entry.outcome}{detail}")
    remaining = len(missed) - MAX_UPLOAD_ENTRY_MESSAGES
    if remaining > 0:
        messages.warning(
            request, f"…and {remaining} more entr(ies) didn't import; see the worker log."
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

        # Recover reporter/title from a pasted (often forwarded) report so the
        # metadata is filled even with no LLM backend configured. Anything the
        # operator typed in the form wins; this only fills the blanks.
        from franktheunicorn.data_access.email_inbox.parser import parse_pasted_report

        parsed = parse_pasted_report(raw_text)

        report = SecurityReport.objects.create(
            raw_text=raw_text,
            title=title or parsed.subject,
            project=project,
            reporter_name=reporter_name or parsed.from_name,
            reporter_email=reporter_email or parsed.from_email,
            source="paste",
        )

        # Queue auto-triage if configured — runs in the worker, not inline.
        try:
            _auto_triage_report(report)
        except Exception:
            logger.warning("Failed to queue auto-triage for report %d", report.pk, exc_info=True)

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

    # Triage runs in the worker now, so its outcome lives on a WorkerCommand
    # row. Nothing rendered that, which meant a failed run looked exactly like
    # one that never happened: the operator was told "queued", reloaded, and
    # found no result and no explanation.
    triage_command = (
        WorkerCommand.objects.filter(command="run_security_triage", security_report=report)
        .order_by("-created_at")
        .first()
    )

    return render(
        request,
        "dashboard/security_detail.html",
        {
            "report": report,
            "sandbox_enabled": sandbox_enabled,
            **_triage_area_context(report, triage_command),
        },
    )


def _triage_area_context(
    report: SecurityReport, triage_command: WorkerCommand | None
) -> dict[str, object]:
    """Context for _security_triage_area.html, shared by the page and the htmx POST.

    Both have to build the same panel or the POST's swap drops parts of it.
    """
    from franktheunicorn.security.queue import in_flight_statuses

    return {
        "report": report,
        "triage_command": triage_command,
        "has_triage_assessment": _has_triage_assessment(report),
        "has_triage_severity": _has_triage_severity(report),
        "triage_in_flight": (
            triage_command is not None and triage_command.status in in_flight_statuses()
        ),
    }


def _render_triage_area(
    request: HttpRequest,
    report: SecurityReport,
    *,
    notice: str = "",
    notice_level: str = "queued",
    notice_link: str = "",
) -> HttpResponse:
    """Re-render the triage panel for an htmx swap.

    Every exit from the triage endpoint goes through here. Returning a bare error
    div instead replaced #triage-area wholesale and took the run button with it,
    so "No LLM backend configured" left nothing to click after fixing the config.
    """
    # Best-effort: this is also the handler for "the database just failed", and
    # re-querying the same table there raised from inside the except block for a
    # 500 that htmx does not swap on — so the click produced no visible change at
    # all, strictly worse than the static error div this replaced.
    triage_command = None
    try:
        triage_command = (
            WorkerCommand.objects.filter(command="run_security_triage", security_report=report)
            .order_by("-created_at")
            .first()
        )
    except Exception:
        logger.debug("Could not re-read triage command for report %d", report.pk, exc_info=True)

    return render(
        request,
        "dashboard/_security_triage_area.html",
        {
            **_triage_area_context(report, triage_command),
            "notice": notice,
            "notice_level": notice_level,
            "notice_link": notice_link,
        },
    )


def _has_triage_assessment(report: SecurityReport) -> bool:
    """Whether triage left anything ``_security_triage_result.html`` can render.

    Every field that partial gates a block on, and nothing else. Not the same
    question as "is triage_summary set": the model asks for a summary but doesn't
    require one, so a run that answered with only a POC assessment used to render
    a blank panel with no way to re-run.
    """
    return bool(
        report.triage_summary
        or report.poc_assessment
        or report.expected_behavior_explanation
        or report.is_expected_behavior
        or report.poc_plausible is not None
    )


def _has_triage_severity(report: SecurityReport) -> bool:
    """Whether the parse step rated the report, which happens two LLM calls earlier.

    Kept apart from the assessment because it can be true on its own — that's the
    commonest partial failure, parse fine and the analysis model unreachable — and
    the two want different words. Folding it into one flag rendered the result
    partial for a report none of whose blocks it can show: an empty "Triage
    Analysis" heading, with the "produced no assessment" explanation suppressed
    because something counted as a result.
    """
    return bool(report.assessed_severity and report.assessed_severity != "unknown")


@require_POST
def security_report_triage(request: HttpRequest, report_id: int) -> HttpResponse:
    """Queue LLM triage on a security report via the worker (htmx)."""
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    try:
        from franktheunicorn.config.loader import get_operator_config

        operator_config = get_operator_config()

        if not operator_config.llm_backends:
            return _render_triage_area(
                request,
                report,
                notice="No LLM backend configured. Add one to operator.yaml.",
                notice_level="failed",
            )

        # _on_request: an explicit click is not "automatic triage", so it is not
        # subject to auto_triage — nor to security_triage.enabled, which defaults
        # off and is commented out in the shipped example config, and would make
        # this button a no-op on a default install.
        from franktheunicorn.security.queue import queue_triage_on_request

        created = queue_triage_on_request(report, operator_config)
        logger.info(
            "%s manual triage for security report #%d",
            "Queued" if created else "Reused in-flight",
            report.pk,
        )
    except Exception:
        logger.exception("Failed to queue triage for report %d", report.pk)
        return _render_triage_area(
            request,
            report,
            notice="Failed to queue triage. Check configuration.",
            notice_level="failed",
        )

    message = (
        "Triage queued — the worker will process it within seconds."
        if created
        else "Triage is already queued for this report."
    )
    return _render_triage_area(
        request,
        report,
        notice=message,
        notice_link=reverse("dashboard:security_detail", args=[report.pk]),
    )


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
def security_report_feedback(request: HttpRequest, report_id: int) -> HttpResponse:
    """Record agree/disagree feedback on a triage verdict (htmx).

    Feeds the iterative learning loop: feedback is distilled into
    ``SecurityTriageGuidance``, which future triage prompts pick up via
    ``security.learning.resolve_triage_guidance``.
    """
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)
    agreed = request.POST.get("agreed", "") == "yes"
    comment = request.POST.get("comment", "").strip()

    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.learning import record_triage_feedback, resolve_triage_guidance

    operator_config = get_operator_config()
    record_triage_feedback(report, agreed, comment, operator_config)
    learned_guidance = resolve_triage_guidance(report.project)

    return render(
        request,
        "dashboard/_security_feedback.html",
        {"report": report, "agreed": agreed, "learned_guidance": learned_guidance},
    )


@require_POST
def security_report_sandbox(request: HttpRequest, report_id: int) -> HttpResponse:
    """Queue a sandbox POC execution for the worker (htmx)."""
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    if not _is_sandbox_enabled():
        return HttpResponse(
            '<div class="sandbox-result" style="color: #c00;">'
            "Sandbox execution is not enabled.</div>"
        )

    # The web container does not have Docker access; enqueue a
    # WorkerCommand for the worker container to pick up and execute.
    WorkerCommand.objects.create(
        command="run_security_sandbox",
        security_report=report,
    )
    return HttpResponse(
        '<div class="sandbox-result" style="color: #1565c0;">'
        "Sandbox run queued. Reload this page in a few minutes to see the verdict.</div>"
    )


@require_POST
def security_report_cve_check(request: HttpRequest, report_id: int) -> HttpResponse:
    """Trigger CVE lookup (htmx)."""
    report = get_object_or_404(SecurityReport, pk=report_id)

    try:
        from franktheunicorn.config.loader import get_operator_config

        operator_config = get_operator_config()

        # Not gated on security_triage.enabled, for the reason spelled out in
        # security.queue.queue_triage_on_request: it defaults False and the
        # example operator.yaml ships the whole block commented out, so on the
        # install our own docs produce this button became a permanent no-op
        # pointing at a key that isn't in the operator's file — while the page it
        # sits on, the paste form and Run LLM Triage all worked. One NVD lookup
        # for the report you're looking at is the click, not automatic behaviour.
        # nvd_api_key_env is optional and search_cves works without it.
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


def security_guidance_list(request: HttpRequest) -> HttpResponse:
    """Overview of learned triage guidance, per project and global.

    Mirrors ``anti_pattern_list`` — a read-only view of what the iterative
    learning loop has distilled from operator agree/disagree feedback so
    far.
    """
    guidance = SecurityTriageGuidance.objects.select_related("project").filter(is_active=True)
    return render(
        request,
        "dashboard/security_guidance.html",
        {"guidance_rows": guidance},
    )


def _auto_triage_report(report: SecurityReport) -> None:
    """Queue a security report for auto-triage via the worker if configured."""
    from franktheunicorn.config.loader import get_operator_config

    operator_config = get_operator_config()

    from franktheunicorn.security.queue import queue_triage_if_enabled

    if queue_triage_if_enabled(report, operator_config):
        logger.info("Queued auto-triage for security report #%d", report.pk)


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

    return ingest_single_pr(owner, repo, pr_number, pace_requests=False)


@require_POST
def run_agents(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Queue an on-demand LLM agent review for the worker (htmx)."""
    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)
    from franktheunicorn.config.loader import get_project_config

    project_config = get_project_config(pr.project.full_name)
    if not project_config:
        return HttpResponse(
            '<div class="run-agents-result" style="color: #c00;">'
            "No project config found for this repo.</div>"
        )
    # Enqueue rather than run inline: process_pr makes external HTTP +
    # LLM calls and can take 30-120s, which would tie up the web request.
    WorkerCommand.objects.create(command="run_agents", pull_request=pr)
    return HttpResponse(
        '<div class="run-agents-result" style="color: #1565c0; margin: 0;">'
        "Agent run queued. Reload this page in a few minutes to see updated findings.</div>"
    )


@require_POST
def run_dual_tests(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Queue a manual differential test run for the worker (htmx).

    The worker container runs the tests inside Docker; the web container
    must not (no Docker socket). Operator reloads the PR detail page to
    see the verdict once the worker drains the command.
    """
    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)

    from franktheunicorn.config.loader import get_project_config

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

    WorkerCommand.objects.create(command="run_dual_tests", pull_request=pr)
    return HttpResponse(
        '<div class="run-tests-result" style="color: #1565c0;">'
        "Test run queued. Reload this page in a few minutes to see the verdict.</div>"
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
