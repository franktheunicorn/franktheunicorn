"""
Dashboard views — server-rendered HTML with htmx interactivity.

Function-based views. No SPA, no React. htmx for all dynamic updates.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.security.sheet_sync import SheetImportResult
    from franktheunicorn.security.zip_import import ZipImportResult

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse

# StreamingHttpResponse is not an HttpResponse — both descend from
# HttpResponseBase — so the streaming CSV view is annotated with the base.
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
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
    SecurityRecheckRun,
    SecurityReport,
    SecurityTriageFeedback,
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
        .filter(state="open", queue=queue, project__enabled=True)
        .filter(Project.configured_q(lookup="project"))
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
    base_qs = PullRequest.objects.filter(state="open", project__enabled=True).filter(
        Project.configured_q(lookup="project")
    )
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

    enabled_projects_qs = Project.objects.filter(enabled=True).filter(Project.configured_q())
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

    # The recorded truth, where there is one. `bool(drafts)` cannot answer this —
    # a reviewer that ran and found nothing leaves exactly what a reviewer that
    # never ran leaves, which is the whole reason PullRequest.agent_runs exists,
    # and until now no view or template read it. So the Agents table showed
    # "not run" in italic grey for a reviewer that had run cleanly, which is the
    # operator-facing symptom ("claude_cli doesn't seem to be getting fired")
    # that started all of this.
    runs = pr.agent_runs or {}

    summary = []
    for source_key, display_name in configured:
        drafts = source_drafts.get(source_key, [])
        run = runs.get(source_key)
        # Falls back to the draft inference for rows that predate the records, so
        # an existing install's history does not all read as "not run".
        did_run = run is not None if runs else bool(drafts)
        run_status = str(run.get("status", "")) if isinstance(run, dict) else ""

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
                # So the table can say "ran, found nothing" and "failed" rather
                # than collapsing both into the absence of findings.
                "run_status": run_status,
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
                '<div class="post-result error-note">'
                "Cannot post: no forge client/token configured.</div>"
            )

        try:
            poster = GitHubPoster(client)  # type: ignore[arg-type]
            poster.post_review(pr, approved)
        finally:
            client.close()  # type: ignore[attr-defined]

        return HttpResponse(
            f'<div class="post-result ok-note">Posted {len(approved)} findings to GitHub.</div>'
        )
    except Exception:
        logger.exception("Failed to post review for PR #%d", pr.number)
        return HttpResponse('<div class="post-result error-note">Failed to post review.</div>')


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
            '<div class="feedback-result error-note">Invalid assessment value.</div>'
        )

    if not feedback_body.strip():
        return HttpResponse(
            '<div class="feedback-result error-note">Feedback body cannot be empty.</div>'
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

    projects = (
        Project.objects.filter(enabled=True)
        .filter(Project.configured_q())
        .order_by("owner", "repo")
    )
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
            '<div class="merge-result error-note">Merge queue not enabled for this project.</div>'
        )

    # Re-verify merge eligibility server-side before executing.
    eligibility = evaluate_merge_eligibility(pr, pc.merge_queue)
    if not eligibility.eligible:
        return HttpResponse(
            f'<div class="merge-result error-note">'
            f"PR is no longer eligible for merge: {eligibility.details}</div>"
        )

    token = getattr(settings, "FRANK_GITHUB_TOKEN", "")
    if not token:
        return HttpResponse(
            '<div class="merge-result error-note">Cannot merge: GITHUB_TOKEN not configured.</div>'
        )

    github_client = GitHubClient(token=token)
    try:
        result = execute_merge(pr, pc.merge_queue, github_client=github_client)
    finally:
        github_client.close()

    if result.success:
        return HttpResponse(
            f'<div class="merge-result ok-note">Merged PR #{pr.number} via {result.method}.</div>'
        )
    return HttpResponse(f'<div class="merge-result error-note">Merge failed: {result.error}</div>')


# --- Security Report Triage ---


SECURITY_STATUS_TABS: list[dict[str, str]] = [
    {"key": "all", "label": "All"},
    *[{"key": k, "label": v} for k, v in SecurityReport.STATUS_CHOICES],
    # Last, after the status tabs, because it cuts across them rather than being
    # one more status: the thing an operator with a CVE id in hand needs is the
    # list of holes that are public-by-assignment and still unfixed.
    {"key": SecurityReport.CVE_NO_BRANCH_FILTER, "label": "CVE, No Branch"},
]


def _security_tab_q(key: str) -> Q:
    """The filter behind one security-list tab.

    One function so the tab's count and the tab's contents can't disagree — they
    were two separate expressions, which is fine while every tab is
    ``status=<key>`` and a bug waiting for the first tab that isn't.

    "all" is an empty ``Q``, not ``None``: ``filter(Q())`` produces byte-identical
    SQL to no filter at all (verified — no WHERE clause emitted), so the callers
    lose a branch each and there is no fast path to forget.
    """
    if key == "all":
        return Q()
    if key == SecurityReport.CVE_NO_BRANCH_FILTER:
        return SecurityReport.cve_without_branch_q()
    # Unknown keys included: a garbage ?status= has always returned an empty list
    # rather than 400ing, and the tab bar still renders to click out of.
    return Q(status=key)


#: Orderings the list offers, and the default.
#:
#: Priority leads because that is the whole point of reading a scanner's ranking
#: at import: 129 findings in arrival order put the run's two HIGHs at positions 3
#: and 94, and the page only shows the first 100. "Newest" stays available because
#: a trickle of emailed reports all rank 0.0 and arrival order is the right one
#: for an inbox.
_SECURITY_SORTS = {
    "priority": ("-priority", "-created_at"),
    "newest": ("-created_at",),
}
_SECURITY_SORT_LABELS = (("priority", "Priority"), ("newest", "Newest"))
_DEFAULT_SECURITY_SORT = "priority"

#: Rows the list renders. Ranked first, so the cap keeps the top of the queue —
#: but it has to be said on the page, not just honoured.
SECURITY_LIST_ROW_CAP = 100


def security_report_list(request: HttpRequest) -> HttpResponse:
    """List security reports with status tabs, ranked highest-priority first."""
    # Normalised, not passed through. Two things went wrong while this was taken on
    # trust, both because the page echoes the value into the Export CSV href:
    #
    # 1. ``?status=new%26full=1`` rendered ``status=new&full=1`` in that href
    #    (autoescaped to ``&amp;``, which the browser decodes back to ``&``), so the
    #    plain Export button handed back the ``--full`` export — raw report text and
    #    proposed patches, the two columns kept opt-in precisely because they are a
    #    working description of how to exploit the thing.
    # 2. ``?status=`` filtered to ``Q(status="")``, which matches nothing, so the
    #    page showed "No reports on this tab" while the href it rendered carried an
    #    empty status the export reads as "no filter" — one click from "this slice is
    #    clear" to mailing a PMC the entire unfixed backlog.
    #
    # Anything unrecognised falls back to "all" rather than 400ing: the tab bar has
    # always rendered for a junk ?status=, and a page whose own Export button 400s is
    # its own bug.
    status_filter = request.GET.get("status", "all")
    if status_filter != "all" and status_filter not in SecurityReport.list_filters():
        status_filter = "all"
    sort = request.GET.get("sort", _DEFAULT_SECURITY_SORT)
    if sort not in _SECURITY_SORTS:
        sort = _DEFAULT_SECURITY_SORT
    reports = SecurityReport.objects.select_related("project").order_by(*_SECURITY_SORTS[sort])

    reports = reports.filter(_security_tab_q(status_filter))

    all_reports = SecurityReport.objects.all()
    tabs_with_counts: list[dict[str, str | int]] = []
    for tab in SECURITY_STATUS_TABS:
        count = all_reports.filter(_security_tab_q(tab["key"])).count()
        tabs_with_counts.append({**tab, "count": count})

    # Read out of the counts just computed rather than counted again: the active
    # tab's COUNT(*) and this one were byte-identical queries, free on the "all"
    # tab (SQLite answers a bare count from the smallest b-tree) and so only
    # wasteful on the filtered tabs, which is every tab anyone works from. Falls
    # back to a count for a ?status= that matches no tab, which renders empty.
    counts_by_key = {str(tab["key"]): int(tab["count"]) for tab in tabs_with_counts}
    filtered_count = counts_by_key.get(status_filter, -1)
    if filtered_count < 0:
        filtered_count = reports.count()

    return render(
        request,
        "dashboard/security_list.html",
        {
            "reports": reports[:SECURITY_LIST_ROW_CAP],
            "status_tabs": tabs_with_counts,
            # So the page can say it is showing 100 of N. The tab badge counts the
            # whole filtered set, and on a tab meant to be driven to zero — "CVE, No
            # Branch" — a badge the visible rows can't account for reads as either a
            # broken count or a queue that won't clear. The CSV cap next to it is a
            # different number about a different thing.
            "row_cap": SECURITY_LIST_ROW_CAP,
            "rows_capped": filtered_count > SECURITY_LIST_ROW_CAP,
            "active_status": status_filter,
            "active_sort": sort,
            "sort_options": _SECURITY_SORT_LABELS,
            "archives": _imported_archives(),
            # Configured projects, plus any project that actually has a report.
            # The reports themselves are deliberately *not* allow-list filtered —
            # hiding a security report because its YAML was removed loses sight of
            # security work, which is worse than showing a row for an unmonitored
            # repo. But then the filter has to be able to reach those rows: with
            # only the allow-list here, a report was listed whose owner/repo could
            # not be selected in the dropdown above it.
            "projects": (
                Project.objects.filter(
                    (Q(enabled=True) & Project.configured_q())
                    | Q(pk__in=SecurityReport.objects.exclude(project=None).values("project"))
                )
                .distinct()
                .order_by("owner", "repo")
            ),
            "zip_import_command": _zip_import_command(),
            # So the page can say the CSV export is capped *before* the operator
            # shares a sheet that quietly stops short of the backlog.
            # Counted against the *filtered* set, not the whole backlog. Using the
            # unfiltered count cried wolf on every status tab: 3,000 reports total
            # with 40 in "new" showed "the export stops at 2000" on a tab that
            # exports 40.
            "export_cap": MAX_SECURITY_CSV_EXPORT_ROWS,
            "export_total": filtered_count,
            "export_capped": filtered_count > MAX_SECURITY_CSV_EXPORT_ROWS,
        },
    )


def _imported_archives() -> list[dict[str, object]]:
    """One row per archive an import came from, newest first.

    Exists for the Drop button. A bad import is a real event — a scan of the wrong
    branch, an archive a shape ahead of the expander, a project picked wrong — and
    until now the only way back was ``manage.py shell``. Grouping on the archive
    label alone (not label-and-project) is deliberate: "drop this archive" should
    mean the whole archive, including the half of it that went in against a
    different project on a second attempt.

    ``touched`` counts reports the operator has already worked on, which is what
    makes dropping unsafe; the confirm text quotes it.
    """
    rows = (
        SecurityReport.objects.exclude(source_archive="")
        .values("source_archive")
        .annotate(
            total=Count("pk"),
            findings=Count("pk", filter=~Q(finding_id="")),
            touched=Count("pk", filter=~Q(status="new")),
            first_seen=Min("created_at"),
            last_seen=Max("created_at"),
        )
        .order_by("-last_seen")
    )
    return list(rows)


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
        if project is None:
            # Parses but doesn't resolve — a stale <option> for a project disabled
            # between render and submit, or a re-posted form. Importing the whole
            # archive with project=None silently defeated the per-project dedup
            # the page advertises as safe: the operator re-uploads with the project
            # selected and every report imports a second time.
            messages.error(
                request, "That project no longer exists — nothing was imported. Pick another."
            )
            return redirect("dashboard:security_list")

    # Off unless the operator ticked the box. A backlog import fans out to an
    # NVD lookup and two LLM calls per report, which is not a thing to start by
    # accident from a file picker.
    auto_triage = request.POST.get("auto_triage") == "on"
    # Off unless ticked, and a sharper knob than triage: this is a full agent run
    # per report per active branch, not two LLM calls.
    auto_verify = request.POST.get("auto_verify") == "on"
    auto_verify_versions = request.POST.get("auto_verify_versions") == "on"
    auto_find_introduction = request.POST.get("auto_find_introduction") == "on"

    # Not gated on the file name: the importer decides by content and reports a
    # non-zip as an error, so a ".ZIP" or an extensionless export still works.
    # No web-specific entry cap: the importer's own MAX_ENTRIES applies to both
    # doors. There used to be one at 200, on the grounds that the whole import
    # runs inside this request against the SQLite file the worker writes — true,
    # but the cost was 2000 separate commits, and the walk is now one transaction.
    # Measured, file-backed: 265 entries went from 1008ms of commits to 6.3ms,
    # 2000 from 8.3s to 27ms, with parsing at 0.03ms an entry. A real 265-entry
    # scanner archive was being refused for a bound that no longer buys anything;
    # the 8 MB upload cap is what limits this door now.
    result = import_reports_from_zip(
        upload,
        project=project,
        auto_triage=auto_triage,
        auto_verify=auto_verify,
        auto_verify_versions=auto_verify_versions,
        auto_find_introduction=auto_find_introduction,
        # The filename is the only provenance a browser upload carries, and two
        # scans of one repo share entry paths, so without it the list has pairs of
        # identically-titled reports from different archives.
        archive_label=upload.name or "",
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
        # Reported only when it was asked for. An operator who didn't tick the box
        # doesn't need telling that a thing they declined didn't happen.
        if auto_verify and result.verify_skipped_reason:
            messages.warning(request, f"Not verified: {result.verify_skipped_reason}.")
        if auto_verify_versions and result.version_map_skipped_reason:
            messages.warning(request, f"Versions not mapped: {result.version_map_skipped_reason}.")
        if auto_find_introduction and result.introduction_skipped_reason:
            messages.warning(request, f"Not dated: {result.introduction_skipped_reason}.")
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


@require_POST
def security_archive_drop(request: HttpRequest) -> HttpResponse:
    """Delete every security report that came from one archive.

    The undo for a bad import. POST-only and matched on the exact
    ``source_archive`` label, never a prefix or an icontains — a fuzzy match here
    deletes reports the operator didn't mean, and reports are the one thing in this
    app with no other copy (the archive on disk has the findings, but not the
    triage verdicts, the operator notes or the feedback rows).

    ``SecurityTriageFeedback.report`` is ``SET_NULL`` so the learning corpus
    distilled from real operator decisions survives — those rows are about the
    operator's judgement, not about the report, and re-importing the archive
    shouldn't have to re-earn them.

    Pending worker jobs are deleted first in this same transaction so a
    worker cannot claim one in the gap and burn an NVD lookup or an agent
    run on a report that no longer exists. Running ones are already claimed;
    CASCADE takes them with the report.
    """
    archive = request.POST.get("archive", "").strip()
    if not archive:
        messages.error(request, "No archive named — nothing was dropped.")
        return _back_to_security_list(request)

    doomed = SecurityReport.objects.filter(source_archive=archive)
    # Counted before the delete: the queryset is empty afterwards, and reporting
    # "0 reports dropped" for a successful drop is how an operator concludes the
    # button is broken and clicks it again.
    report_ids = list(doomed.values_list("pk", flat=True))
    total = len(report_ids)
    if not total:
        messages.warning(request, f"No reports left from {archive}.")
        return _back_to_security_list(request)

    touched = doomed.exclude(status="new").count()
    from franktheunicorn.security.queue import cancel_pending_for_reports

    with transaction.atomic():
        cancelled = cancel_pending_for_reports(report_ids)
        doomed.delete()
    bits: list[str] = []
    if cancelled:
        bits.append(f"cancelled {cancelled} queued job(s)")
    if touched:
        bits.append(f"{touched} had already been triaged or ruled on")
    note = f" ({'; '.join(bits)})" if bits else ""
    messages.success(request, f"Dropped {total} report(s) from {archive}{note}.")
    logger.info(
        "Dropped %d security report(s) from archive %s; cancelled %d pending command(s)",
        total,
        archive,
        cancelled,
    )
    return _back_to_security_list(request)


def _back_to_security_list(request: HttpRequest) -> HttpResponse:
    """Reload the security list, whether htmx made the request or the form did.

    Every other htmx endpoint here swaps a fragment, and this one can't: dropping
    an archive changes its own row, the report list and all eight tab counts. A
    fragment would leave the operator looking at counts that no longer match the
    list. ``HX-Redirect`` asks htmx to do a full navigation instead, which is also
    what makes the flash message land — a swapped fragment never renders one.

    The plain ``redirect`` is the no-JS path: the button sits in a real form with a
    real action, so it works with htmx switched off.
    """
    target = reverse("dashboard:security_list")
    if request.headers.get("HX-Request") == "true":
        return HttpResponse(status=204, headers={"HX-Redirect": target})
    return redirect(target)


def _branch_sweep_gate_reason(operator_config: OperatorConfig) -> str:
    """Why neither git sweep can run, or "" when they can.

    They borrow the verifier's checkout, so they inherit exactly its two
    prerequisites — and asked here rather than in the worker so a press that
    cannot possibly work says why on the page, instead of queuing a command that
    no-ops into a log nobody is tailing.
    """
    from franktheunicorn.security.queue import verifier_gate_reason

    reason = verifier_gate_reason(operator_config)
    if reason:
        return f"The git sweeps borrow the verifier's checkout, and it can't run: {reason}."
    return ""


def _still_unruled(report: SecurityReport) -> bool:
    """Whether *report* is still unruled **as stored**, not as this loop last saw it.

    The bulk buttons materialise their candidates up front and then take real
    wall-clock time — thousands of reports, a queue write and sometimes a procedural
    close each — so a verdict the operator types at minute three is not in a snapshot
    taken at minute zero. ``procedural_close_if_evidence`` re-reads status for exactly
    this reason. One query, spent only where the next line would otherwise bill an
    LLM call or a coding-agent run.
    """
    return not (
        SecurityReport.objects.filter(pk=report.pk)
        .filter(SecurityReport.operator_has_ruled_q())
        .exists()
    )


@require_POST
def security_report_rerun_triage(request: HttpRequest) -> HttpResponse:
    """Re-run triage across the queue, procedural close first then LLM (htmx, bulk).

    For every report the operator hasn't ruled on and that carries no CVE: run
    the procedural auth-disabled close synchronously (zero LLM cost), and if it
    doesn't close, queue LLM triage. The worker chains the version follow-on
    (cheap git map + deep verifier) for the ones triage rules valid-looking —
    so this view doesn't bill agent runs on reports triage is about to call
    invalid.

    Also queues the version follow-on for reports the operator already ruled
    *valid* but never wrote down affected versions — the second question
    ("where does it ship") is worth answering on a confirmed hole even when the
    first ("is it real") was settled by hand. Skipped for invalid / duplicate /
    expected-behavior: no hole to map, and the operator already said so.
    """
    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.queue import (
        PRIORITY_BULK,
        in_flight_statuses,
        queue_triage,
        queue_version_follow_on,
    )
    from franktheunicorn.security.triage import procedural_close_if_evidence

    operator_config = get_operator_config()
    if not operator_config.llm_backends:
        messages.error(request, "No LLM backend configured. Add one to operator.yaml.")
        return _back_to_security_list(request)

    # The working set: reports the machine may touch (new / triaging) plus
    # operator-ruled-valid ones that might still need version work. Everything
    # else is out of scope — an operator who ruled invalid/duplicate is done.
    candidates = list(
        SecurityReport.objects.select_related("project")
        .filter(status__in=["new", "triaging", "valid"])
        .order_by("-priority", "created_at")
    )

    # Which reports already have a triage run queued or running, so the loop can
    # leave them alone. Re-closing one of those procedurally would race the
    # in-flight LLM run: the close writes auto_triage_status="invalid", the worker
    # finishes a moment later and overwrites it with its own verdict — the cheap,
    # evidence-based close loses to a guess. The worker does not re-run the
    # procedural close itself (its never-been-triaged gate skips re-triage), so
    # the only way that verdict lands is this button, and only when nothing is
    # already in flight on the row.
    inflight_ids = set(
        WorkerCommand.objects.filter(
            command="run_security_triage", status__in=in_flight_statuses()
        ).values_list("security_report_id", flat=True)
    )

    procedural_closed = 0
    triage_queued = 0
    triage_skipped_inflight = 0
    followon_queued = 0
    followon_skipped = 0
    followon_inflight = 0
    ruled_skipped = 0
    for report in candidates:
        is_auto = report.status in ("new", "triaging")
        if is_auto and not report.operator_has_ruled:
            if report.pk in inflight_ids:
                # A triage run is already under way — don't race it.
                triage_skipped_inflight += 1
                continue
            # retrigger=True so the close reaches reports the first pass missed —
            # including ones the LLM path already assessed and left sitting in
            # ``new`` with a staged verdict. Same door the procedural-only button
            # uses; without it the full re-triage closed 0 where the standalone
            # button closed 10, because the never-been-triaged gate skipped
            # exactly the reports a re-triage is for.
            if procedural_close_if_evidence(report, retrigger=True):
                procedural_closed += 1
                continue
            if not _still_unruled(report):
                ruled_skipped += 1
                continue
            if queue_triage(report, priority=PRIORITY_BULK):
                triage_queued += 1
            else:
                triage_skipped_inflight += 1
        # Not gated on operator_has_ruled: a valid report *with* a CVE is precisely
        # what version mapping is for, so the general ruled test would turn the
        # follow-on off for its main case. A recorded fix branch is the narrow signal
        # that the work is done, and this path is the expensive one — the verifier
        # bills a coding-agent run per active release branch.
        elif (
            report.status == "valid" and not report.affected_versions and not report.fixed_in_branch
        ):
            vm, verify, skipped = queue_version_follow_on(report, operator_config)
            if skipped:
                followon_skipped += 1
            elif vm or verify:
                followon_queued += 1
            else:
                # Both commands declined because one was already in flight
                # (the in-flight dedup, not a gate). Surface it so "the button
                # did nothing" isn't indistinguishable from "it queued work".
                followon_inflight += 1
        else:
            ruled_skipped += 1

    parts = [
        f"{procedural_closed} closed without a model (auth-disabled)",
        f"{triage_queued} queued for LLM triage",
    ]
    if triage_skipped_inflight:
        parts.append(f"{triage_skipped_inflight} already had triage in flight")
    if followon_queued:
        parts.append(f"{followon_queued} queued for version mapping + verification")
    if followon_inflight:
        parts.append(f"{followon_inflight} version follow-on already in flight")
    if followon_skipped:
        parts.append(f"{followon_skipped} version follow-on skipped (verifier off or no project)")
    if ruled_skipped:
        parts.append(f"{ruled_skipped} skipped (operator-ruled: notes, a CVE, or a fix branch)")
    messages.success(request, "Re-ran triage: " + ", ".join(parts) + ".")
    logger.info(
        "Bulk re-triage: procedural_closed=%d triage_queued=%d triage_inflight=%d "
        "followon_queued=%d followon_inflight=%d followon_skipped=%d ruled_skipped=%d",
        procedural_closed,
        triage_queued,
        triage_skipped_inflight,
        followon_queued,
        followon_inflight,
        followon_skipped,
        ruled_skipped,
    )
    return _back_to_security_list(request)


@require_POST
def security_report_rerun_triage_failed(request: HttpRequest) -> HttpResponse:
    """Re-queue triage for reports whose last triage run *failed* (htmx, bulk).

    The full re-triage button walks every unruled report, which on a big backlog
    is a lot of LLM calls to reach the handful that actually need another
    attempt. This one touches only reports whose most recent run_security_triage
    command failed — the model was down, the answer didn't parse — and leaves
    reports with a verdict, a ruling, or a clean run alone.

    Same manners as the full pass: the free procedural close gets first crack,
    and anything the operator has ruled on since (a status, notes, a CVE) is
    skipped — a failure doesn't make the report unruled.
    """
    from django.db.models import OuterRef, Subquery

    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.queue import PRIORITY_BULK, queue_triage
    from franktheunicorn.security.triage import procedural_close_if_evidence

    operator_config = get_operator_config()
    if not operator_config.llm_backends:
        messages.error(request, "No LLM backend configured. Add one to operator.yaml.")
        return _back_to_security_list(request)

    latest_triage = WorkerCommand.objects.filter(
        command="run_security_triage", security_report=OuterRef("pk")
    ).order_by("-created_at")
    candidates = list(
        SecurityReport.objects.select_related("project")
        .annotate(last_triage_status=Subquery(latest_triage.values("status")[:1]))
        .filter(last_triage_status="failed")
        .order_by("-priority", "created_at")
    )

    requeued = 0
    procedural_closed = 0
    ruled_skipped = 0
    for report in candidates:
        if report.status not in ("new", "triaging") or report.operator_has_ruled:
            ruled_skipped += 1
            continue
        if not _still_unruled(report):
            ruled_skipped += 1
            continue
        if procedural_close_if_evidence(report, retrigger=True):
            procedural_closed += 1
            continue
        # queue_triage dedups on anything already in flight, so a report that
        # got re-queued between page load and click is not queued twice.
        if queue_triage(report, priority=PRIORITY_BULK):
            requeued += 1

    parts = [f"{requeued} re-queued"]
    if procedural_closed:
        parts.append(f"{procedural_closed} closed without a model (auth-disabled)")
    if ruled_skipped:
        parts.append(
            f"{ruled_skipped} skipped (operator-ruled: notes, a CVE, or a fix branch, set since)"
        )
    if not candidates:
        parts = ["no failed triage runs in the queue"]
    messages.success(request, "Re-run failed triage: " + ", ".join(parts) + ".")
    logger.info(
        "Bulk re-triage of failures: requeued=%d procedural_closed=%d ruled_skipped=%d",
        requeued,
        procedural_closed,
        ruled_skipped,
    )
    return _back_to_security_list(request)


@require_POST
def security_report_rerun_procedural(request: HttpRequest) -> HttpResponse:
    """Re-run only the cheap procedural close across the queue (htmx, bulk, no LLM).

    The lighter sibling of the full re-triage button: runs the auth-disabled
    regex close on every report the operator hasn't ruled on, and queues
    nothing. Zero LLM cost, zero agent runs — the one bulk action that works
    with no backend configured at all. Useful to re-apply the close after a
    regex fix (the OR-precondition guard, say) to reports the first pass missed,
    including ones the LLM path already assessed and left in ``new`` with a
    staged verdict.

    Skips operator-ruled reports (non-``new`` status, operator notes, or a
    CVE) and in-flight ``triaging`` reports (the worker is mid-LLM on those;
    re-closing would race it). Re-opens are the operator's call.
    """
    from franktheunicorn.security.triage import procedural_close_if_evidence

    candidates = list(
        SecurityReport.objects.select_related("project")
        .filter(status="new")
        .order_by("-priority", "created_at")
    )

    closed = 0
    ruled_skipped = 0
    for report in candidates:
        if report.operator_has_ruled:
            ruled_skipped += 1
            continue
        if procedural_close_if_evidence(report, retrigger=True):
            closed += 1

    parts = [f"{closed} closed without a model (auth-disabled)"]
    if ruled_skipped:
        parts.append(f"{ruled_skipped} skipped (operator-ruled: notes, a CVE, or a fix branch)")
    messages.success(request, "Re-ran procedural close: " + ", ".join(parts) + ".")
    logger.info("Bulk procedural re-trigger: closed=%d ruled_skipped=%d", closed, ruled_skipped)
    return _back_to_security_list(request)


@require_POST
def security_report_rerun_duplicates(request: HttpRequest) -> HttpResponse:
    """Re-run duplicate detection across the whole backlog (htmx, bulk, one LLM pass).

    Asks the model to group the backlog's titles — one call per project per few
    hundred reports, so a handful of completions in-request rather than a worker
    command (no container, no per-pair calls). Links the groups it calls out and
    clears stale auto-links whose two titles the model saw together and did not
    group — the latter is the point: a re-check that only ever added links would
    leave the false positives from a buggy heuristic on the rows forever.
    Hand-set links are never touched.

    Runs regardless of ``security_triage.duplicates.enabled``: that flag gates the
    automatic triage-time path, and an operator pressing a button has asked for it.
    """
    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.duplicates import redetect_across_backlog
    from franktheunicorn.security.triage import resolve_triage_backend

    operator_config = get_operator_config()
    backend = resolve_triage_backend(operator_config)
    if backend is None:
        messages.error(request, "No LLM backend configured. Add one to operator.yaml.")
        return _back_to_security_list(request)

    config = operator_config.security_triage.duplicates
    candidates = list(SecurityReport.objects.select_related("project", "duplicate_of"))

    result = redetect_across_backlog(candidates, config, backend)
    if result is None:
        messages.error(
            request,
            "Duplicate re-check failed — every LLM call errored or returned nothing "
            "parseable. Existing links were left alone; see the log.",
        )
        return _back_to_security_list(request)
    linked, cleared = result

    parts = [f"{linked} linked"]
    if cleared:
        parts.append(f"{cleared} stale link(s) cleared")
    messages.success(request, "Re-checked duplicates: " + ", ".join(parts) + ".")
    logger.info("Bulk duplicate re-check (LLM): linked=%d cleared=%d", linked, cleared)
    return _back_to_security_list(request)


#: Cap on the CSV export. Not a performance bound — the export streams, so the
#: size it can serve is unbounded — but a review bound: a sheet nobody is going
#: to read to the bottom is a sheet whose bottom half gets rubber-stamped, and
#: the ranking already puts what matters at the top. The CLI has --limit and no
#: default cap for the operator who really does want the lot.
MAX_SECURITY_CSV_EXPORT_ROWS = 2000

#: Same 8 MB as the zip door. A reviewed export is a few hundred KB; anything
#: this size is a mistake or a different file altogether.
MAX_SECURITY_CSV_UPLOAD_BYTES = 8 * 1024 * 1024


def security_report_export_csv(request: HttpRequest) -> HttpResponseBase:
    """Stream the security backlog as a CSV for review in a shared spreadsheet.

    The collaboration path for a backlog that isn't one person's to rule on: the
    operator drops this into a Google Sheet (or anything else), shares it with
    whoever decides — a PMC, a co-maintainer — and imports their edits back
    through :func:`security_report_import_csv`. Nothing here talks to Google;
    see :mod:`franktheunicorn.security.sheet_sync` for why not.

    Streams rather than building the file, and stays in-request because it's a
    read: no LLM call, no container, no write. Honours the same ``status`` *and*
    ``sort`` the list page is using, so "export what I'm looking at" is one click
    and means it — forwarding only the status re-ranked the sheet under the
    operator, and with the row cap on top, kept a different set of reports than
    the ones they were looking at.
    """
    from franktheunicorn.security.sheet_sync import (
        DEFAULT_EXPORT_SORT,
        EXPORT_SORTS,
        export_filename,
        reports_for_export,
        stream_reports_csv,
    )

    # "all" is the list page's own name for no filter, and it arrives here
    # whenever the operator exports from the default tab.
    status = request.GET.get("status", "")
    if status == "all":
        status = ""
    if status and status not in SecurityReport.list_filters():
        # A bad tab in a hand-edited URL. Exporting everything instead would hand
        # back a wider sheet than the operator asked for, which is the wrong way
        # to be wrong about a security backlog.
        return HttpResponse("Unknown status filter.", status=400)

    sort = request.GET.get("sort", DEFAULT_EXPORT_SORT)
    if sort not in EXPORT_SORTS:
        sort = DEFAULT_EXPORT_SORT
    full = request.GET.get("full") == "1"
    reports = reports_for_export(status=status, limit=MAX_SECURITY_CSV_EXPORT_ROWS, sort=sort)

    # .iterator() so a big backlog isn't fully materialised to serve a stream.
    response = StreamingHttpResponse(
        stream_reports_csv(reports.iterator(chunk_size=200), full=full),
        content_type="text/csv; charset=utf-8",
    )
    filename = export_filename(full=full, status=status)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    # This file is unfixed vulnerability reports, and with full=1 it's a working
    # description of how to exploit them. Without no-store it's heuristically
    # cacheable: it sits in the disk cache of whatever laptop reached the
    # dashboard over Tailscale, recoverable long after the operator thinks the
    # download was deleted.
    response["Cache-Control"] = "no-store"
    logger.info(
        "Security CSV export: status=%s sort=%s full=%s (cap %d rows)",
        status or "all",
        sort,
        full,
        MAX_SECURITY_CSV_EXPORT_ROWS,
    )
    return response


@require_POST
def security_report_import_csv(request: HttpRequest) -> HttpResponse:
    """Apply a reviewed CSV back onto the reports it was exported from.

    Runs in-request like the zip door and for the same reasons: one transaction,
    bounded by the upload cap, no container and no LLM call. Refuses rows whose
    report changed after the export rather than picking a winner — see
    :func:`franktheunicorn.security.sheet_sync.import_reports_csv`.
    """
    from franktheunicorn.security.sheet_sync import import_reports_csv

    upload = request.FILES.get("csv_file")
    if upload is None:
        messages.error(request, "Choose a reviewed .csv file to import.")
        return _back_to_security_list(request)

    if upload.size and upload.size > MAX_SECURITY_CSV_UPLOAD_BYTES:
        limit_mb = MAX_SECURITY_CSV_UPLOAD_BYTES // (1024 * 1024)
        messages.error(request, f"That file is larger than the {limit_mb} MB upload limit.")
        return _back_to_security_list(request)

    try:
        # utf-8-sig: Sheets and Excel both write a BOM on a CSV download, and
        # without stripping it the first header becomes "﻿report_id" and the
        # whole file reads as "not an export from here".
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        messages.error(
            request,
            "That file isn't UTF-8 text. Download the sheet as "
            "Comma-separated values, not as .xlsx or .ods.",
        )
        return _back_to_security_list(request)

    # Forcing from the browser is deliberately possible but explicit: the
    # checkbox is the only way to overwrite a ruling made after the export, and
    # it says so.
    force = request.POST.get("force") == "on"
    dry_run = request.POST.get("dry_run") == "on"

    # The whole string, not text.splitlines() — that deletes the newline inside a
    # quoted multi-line cell and runs the words either side of it together, which
    # is every operator_notes cell in the sheet. import_reports_csv splits it.
    result = import_reports_csv(text, dry_run=dry_run, force=force)

    if result.error:
        messages.error(request, f"Import failed: {result.error}")
        return _back_to_security_list(request)

    level = messages.warning if (result.conflicts or result.failed) else messages.success
    level(request, result.summary())
    for warning in result.warnings:
        messages.warning(request, warning)
    _report_rejected_rows(request, result)
    return _back_to_security_list(request)


def _report_rejected_rows(request: HttpRequest, result: SheetImportResult) -> None:
    """Name every row the operator has to look at, capped the way the zip door caps.

    ``needs_attention``, not "didn't apply". Filtering on outcome alone skipped
    the rows that applied *by overwriting a newer ruling* — the loudest thing this
    importer does — so a forced import flashed a bare "applied 1" and the
    operator's own verdict was gone with nothing naming which report. Same for a
    note left unwritten because the sheet only carried a prefix of it. This is
    the mistake ``_REPORTED_ENTRY_OUTCOMES`` below already documents for the zip
    door: a dropped edit is dropped whether the importer calls it a failure or a
    decision.
    """
    notable = [row for row in result.rows if row.needs_attention]
    for row in notable[:MAX_UPLOAD_ENTRY_MESSAGES]:
        target = f"report {row.report_id}" if row.report_id else "no report"
        fields = f" [{', '.join(row.changed)}]" if row.changed else ""
        detail = f" — {row.detail}" if row.detail else ""
        messages.warning(request, f"Row {row.row} ({target}): {row.outcome}{fields}{detail}")
    remaining = len(notable) - MAX_UPLOAD_ENTRY_MESSAGES
    if remaining > 0:
        messages.warning(request, f"…and {remaining} more row(s) worth a look.")


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

    projects = (
        Project.objects.filter(enabled=True)
        .filter(Project.configured_q())
        .order_by("owner", "repo")
    )
    return render(
        request,
        "dashboard/security_create.html",
        {"projects": projects},
    )


def security_report_detail(request: HttpRequest, report_id: int) -> HttpResponse:
    """Detail view for a single security report."""
    # duplicate_of is select_related and the reverse `duplicates` prefetched: the
    # template renders both directions of the link, and without these that's one
    # query for the parent plus one per report pointing at this one.
    report = get_object_or_404(
        SecurityReport.objects.select_related("project", "duplicate_of").prefetch_related(
            "duplicates"
        ),
        pk=report_id,
    )

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

    # branch_order, recorded by the run. Not created_at (every row of a run shares
    # a timestamp, so the database picks), not order_by("branch") (sorts `master`
    # after every `branch-*`), and not a ("main","master","trunk") guess in here —
    # the verifier already resolved the real default from origin/HEAD, so guessing
    # again would sort the default row last for any project on `develop` or `2.x`.
    verifications = list(report.verifications.order_by("branch_order", "branch"))

    # The branch table answers "did you look and what did you find"; this answers
    # "so what do I write in the advisory", which is a different sentence and the
    # one the operator is actually here for. Assembled in the view rather than the
    # template because merging conflicting answers across branches is a decision,
    # not a loop.
    from franktheunicorn.security.verifier import version_rollup
    from franktheunicorn.security.version_map import VERSION_MAP_AGENT

    version_rows = version_rollup(verifications)

    return render(
        request,
        "dashboard/security_detail.html",
        {
            "report": report,
            "sandbox_enabled": sandbox_enabled,
            "verifications": verifications,
            "has_version_map": any(v.agent == VERSION_MAP_AGENT for v in verifications),
            "version_rows": version_rows,
            "affected_versions": [
                row["name"] for row in version_rows if row["status"] == "affected"
            ],
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
        # The flag alone, not the explanation. The partial renders that text only
        # inside {% if report.is_expected_behavior %}, so a run writing *only* the
        # explanation counted as a result the partial then declined to show — an
        # empty "Triage Analysis" heading with the re-run notice suppressed. (An
        # earlier pass at this added `is_expected_behavior and explanation`
        # alongside, which the bare flag below already subsumes; two clauses where
        # one is dead just makes the next reader guess which is load-bearing.)
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
        from franktheunicorn.security.queue import (
            PRIORITY_INTERACTIVE,
            queue_triage_on_request,
        )

        created = queue_triage_on_request(report, operator_config, priority=PRIORITY_INTERACTIVE)
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
def security_report_accept_triage(request: HttpRequest, report_id: int) -> HttpResponse:
    """Promote the machine's staged triage verdict into ``status`` (htmx).

    The Agree button. Triage writes its suggested verdict to
    ``auto_triage_status`` and leaves ``status`` alone; this copies the
    suggestion into ``status`` and clears the staging field, so the report
    moves into the operator's queue for that verdict and the suggestion stops
    being offered. A re-triage later would populate it again.
    """
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    suggestion = report.auto_triage_status.strip()
    if not suggestion:
        return _render_triage_area(
            request,
            report,
            notice="No triage suggestion to accept. Run triage first.",
            notice_level="failed",
        )

    valid_statuses = {choice[0] for choice in SecurityReport.STATUS_CHOICES}
    if suggestion not in valid_statuses:
        # Defensive: triage only ever writes valid STATUS_CHOICES values, but
        # the staging field is a free CharField, so a corrupt row gets a message
        # rather than a 500.
        return _render_triage_area(
            request,
            report,
            notice=f"The staged suggestion {suggestion!r} is not a valid status.",
            notice_level="failed",
        )

    # An Agree click is agreement the guidance loop can learn from, same as the
    # feedback widget's.
    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.learning import record_triage_feedback

    record_triage_feedback(report, True, "", get_operator_config(), distill=False)

    report.status = suggestion
    report.auto_triage_status = ""
    report.save(update_fields=["status", "auto_triage_status", "updated_at"])
    logger.info("Accepted triage suggestion %r for security report #%d", suggestion, report.pk)

    return _render_triage_area(
        request,
        report,
        notice=f"Accepted — report moved to {report.get_status_display()}.",
        notice_link=reverse("dashboard:security_detail", args=[report.pk]),
    )


def _affected_version_names(report: SecurityReport) -> list[str]:
    """The release-line names the verification/version-map rollup calls affected.

    Shared by the detail page and the versions POST endpoint so the "Copy from
    verification" button copies the same rollup the page renders.
    """
    from franktheunicorn.security.verifier import version_rollup

    verifications = list(report.verifications.order_by("branch_order", "branch"))
    version_rows = version_rollup(verifications)
    return [row["name"] for row in version_rows if row["status"] == "affected"]


def _render_versions_area(
    request: HttpRequest,
    report: SecurityReport,
    *,
    notice: str = "",
    notice_level: str = "queued",
) -> HttpResponse:
    return render(
        request,
        "dashboard/_security_versions.html",
        {
            "report": report,
            "affected_versions": _affected_version_names(report),
            "notice": notice,
            "notice_level": notice_level,
        },
    )


@require_POST
def security_report_versions(request: HttpRequest, report_id: int) -> HttpResponse:
    """Set the operator's affected-versions field, or seed it from verification (htmx).

    Two actions on one endpoint: ``copy`` writes the verification/version-map
    rollup (the affected release lines from the branch table) into the field,
    and ``save`` writes the operator's edited textarea. Both re-render the
    partial. The field is the operator's verdict — the agent's rollup is a
    suggestion, never an overwrite — so ``copy`` is a seed, not a live link.
    """
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)
    action = request.POST.get("action", "save")

    if action == "copy":
        names = _affected_version_names(report)
        if not names:
            return _render_versions_area(
                request,
                report,
                notice="No affected rows from verification to copy yet — run Verify first.",
                notice_level="failed",
            )
        report.affected_versions = ", ".join(names)
        report.save(update_fields=["affected_versions", "updated_at"])
        logger.info("Copied %d affected version(s) onto report #%d", len(names), report.pk)
        return _render_versions_area(
            request,
            report,
            notice="Copied from verification — edit above if the advisory needs different wording.",
        )

    # save: take the operator's text as-is. Empty is allowed (clears the field).
    # Cleaned, not just stripped: same class of input as the branch field below, and
    # this one is an export column too, so a NUL stored here rides into the CSV a PMC
    # opens. Multi-line, so newlines survive.
    from franktheunicorn.security.sheet_sync import clean_multi_line

    report.affected_versions = clean_multi_line(request.POST.get("affected_versions", ""))
    report.save(update_fields=["affected_versions", "updated_at"])
    logger.info(
        "Saved affected versions for report #%d (%d chars)",
        report.pk,
        len(report.affected_versions),
    )
    return _render_versions_area(request, report, notice="Saved.")


#: What Save Verdict writes. Named once because the two save paths below differ
#: only by ``auto_triage_status``, and a field added to one list and not the other
#: is a field that silently doesn't persist on half the reports.
_VERDICT_FIELDS = (
    "status",
    "operator_notes",
    "matched_cve_id",
    "fixed_in_branch",
    "branch_match_applied",
)


@require_POST
def security_report_verdict(request: HttpRequest, report_id: int) -> HttpResponse:
    """Set operator verdict on a security report (htmx)."""
    report = get_object_or_404(SecurityReport, pk=report_id)
    new_status = request.POST.get("status", "")
    # Presence-guarded, like the CVE and the branch below: read with a default, a
    # POST that doesn't carry the textarea blanks it. That matters because
    # ``operator_notes`` is in sheet_sync.WRITABLE_COLUMNS, so it has the same
    # second writer that made the CVE guard necessary — an imported ruling should
    # not be destroyed by a submission that never showed the operator the field.
    notes = report.operator_notes
    if "operator_notes" in request.POST:
        notes = request.POST["operator_notes"]

    valid_statuses = {choice[0] for choice in SecurityReport.STATUS_CHOICES}
    if new_status not in valid_statuses:
        return HttpResponse("Invalid status.", status=400)

    # Read before the assignment below: whether the CVE reference still means
    # anything depends on where the status came *from*, not just where it lands.
    was_duplicate = report.status == "duplicate"
    report.status = new_status
    report.operator_notes = notes

    # Take what the form submitted, whatever the status. This used to blank the
    # CVE for every status but "duplicate", which was a harmless tidy-up while
    # this form was the only thing that could write the field — and became silent
    # data loss the moment the review sheet could too (security.sheet_sync): a PMC
    # rules "valid, and here's the related CVE", the operator opens the report,
    # hits Save Verdict without touching a thing, and the reference is gone,
    # including out of the next export. Guarded on presence rather than read with
    # a default, so a POST that doesn't carry the field can't blank it either.
    # Did the operator *change* the value, or did the form just echo back what was
    # already there? That is the real distinction, and testing field presence got
    # it wrong twice in opposite directions.
    #
    # Blanking unconditionally after the assignment deleted a CVE typed in the same
    # submission ("distinct valid bug, related to CVE-2026-2222" saved with an empty
    # reference). Making it an `elif` on presence then made it dead code, because
    # _security_verdict.html always renders the input — so *every* dashboard POST
    # carries the field, the drop never fired, and a duplicate's CVE stuck to a
    # report just ruled valid and rendered as a badge on the list page. The test
    # covering that branch posted a shape the UI never produces, so the suite was
    # green on a path nothing reached.
    previous_cve = report.matched_cve_id
    submitted_cve = previous_cve
    if "matched_cve_id" in request.POST:
        # Validated with sheet_sync's own pattern, not a looser one: this column
        # has two writers and they must agree about what fits in it. Bounded
        # because the field is CharField(max_length=50) and SQLite doesn't enforce
        # that — a 309-character "CVE" persisted here and would be a DataError,
        # i.e. an unhandled 500 losing the status and notes in the same save(), on
        # the Postgres install DATABASE_URL promises.
        from franktheunicorn.security.sheet_sync import looks_like_cve

        candidate = request.POST["matched_cve_id"].strip()
        if candidate and not looks_like_cve(candidate):
            return HttpResponse("That doesn't look like a CVE id (CVE-2026-1234).", status=400)
        submitted_cve = candidate.upper()

    if submitted_cve != previous_cve:
        # The operator said something. Honour it, whatever the status.
        report.matched_cve_id = submitted_cve
    elif was_duplicate and new_status != "duplicate":
        # Untouched, and no longer a duplicate: "actually valid" means the CVE this
        # duplicated no longer describes it.
        report.matched_cve_id = ""
    else:
        report.matched_cve_id = submitted_cve

    # Which branch carries the fix. Cleaned rather than rejected: the characters
    # worth worrying about here — a NUL, a zero-width space carried along by a copy
    # out of a rendered page, a newline from a two-line paste — are ones the operator
    # cannot see, so a 400 tells them nothing, and htmx does not swap a non-2xx body
    # anyway. Same normaliser the importer uses, so the column's two write paths
    # agree about what fits in it. Cleared by submitting it empty, which is how a
    # branch that turned out not to fix it gets taken back off.
    if "fixed_in_branch" in request.POST:
        from franktheunicorn.security.sheet_sync import clean_single_line

        submitted_branch = clean_single_line(request.POST["fixed_in_branch"])
        # Whose answer the field now holds. ``branch_match_applied`` means "the git
        # sweep wrote this", and ``operator_has_ruled`` reads it to decide that a
        # machine tie is not a ruling — so a branch the operator typed over the
        # top of one has to clear the flag, or their own answer keeps not counting
        # and the report stays in every bulk path.
        #
        # Submitting it *empty* deliberately leaves the flag set: that is the
        # documented rejection, and the flag plus an empty field is the only record
        # that the sweep already offered this branch and was told no. See
        # ``branch_scan._apply``, which reads exactly that pair.
        if submitted_branch and submitted_branch != report.branch_match_branch:
            report.branch_match_applied = False
        report.fixed_in_branch = submitted_branch

    # The operator ruled, so the machine's staged suggestion is consumed:
    # leaving it would re-offer an Agree button for a verdict the operator just
    # overrode. A re-triage later would populate it again if wanted.
    if report.auto_triage_status:
        # The ruling is also learning material: whether the operator's verdict
        # matched the staged one is exactly the agree/disagree signal the
        # guidance loop runs on, and most rulings never get a feedback-widget
        # click.
        from franktheunicorn.config.loader import get_operator_config
        from franktheunicorn.security.learning import record_triage_feedback

        record_triage_feedback(
            report,
            report.auto_triage_status.strip() == new_status,
            notes,
            get_operator_config(),
            distill=False,
        )
        report.auto_triage_status = ""
        report.save(update_fields=[*_VERDICT_FIELDS, "auto_triage_status", "updated_at"])
    else:
        report.save(update_fields=[*_VERDICT_FIELDS, "updated_at"])

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
            '<div class="sandbox-result error-note">Sandbox execution is not enabled.</div>'
        )

    # The web container does not have Docker access; enqueue a WorkerCommand for
    # the worker to pick up. Through security.queue, not objects.create: the
    # in-flight constraint covers every command type, and the worker only sets
    # sandbox_requested when the run *finishes*, so any reload re-offers the
    # button and the second click was an uncaught IntegrityError — a 500 that htmx
    # doesn't swap, so it looked like the click did nothing.
    from franktheunicorn.security.queue import PRIORITY_INTERACTIVE, queue_command

    created = queue_command("run_security_sandbox", report, priority=PRIORITY_INTERACTIVE)
    message = (
        "Sandbox run queued. Reload this page in a few minutes to see the verdict."
        if created
        else "A sandbox run is already queued for this report."
    )
    return HttpResponse(f'<div class="sandbox-result queued-note">{message}</div>')


@require_POST
def security_report_verify(request: HttpRequest, report_id: int) -> HttpResponse:
    """Queue the deep verifier: go and read the code, is this vulnerability real (htmx).

    Not the same question as triage. Triage rules on the report; this puts an agent
    in a checkout of the project — a distinct one, so it can check out release
    branches without disturbing the review pipeline's tree — and has it look, once
    per active branch.

    Queued rather than run in-request for the obvious reason: it is minutes of
    agent time per branch. The gate is reported rather than hidden, because a
    button that silently does nothing when ``verifier.enabled`` is false is the
    exact failure this codebase keeps writing rules about.
    """
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.queue import PRIORITY_INTERACTIVE, queue_verification
    from franktheunicorn.security.verifier import resolve_verifier_reviewer

    operator_config = get_operator_config()
    verifier = operator_config.security_triage.verifier
    if not verifier.enabled:
        return render(
            request,
            "dashboard/_security_verify_queued.html",
            {
                "report": report,
                "blocked": (
                    "Verification has been switched off. It defaults on, so something "
                    "set security_triage.verifier.enabled: false in operator.yaml — or "
                    "the file failed to load and took every other setting with it "
                    "(run `manage.py show_config` to tell those apart)."
                ),
            },
        )
    # Checked here rather than left to the worker because it is the one remaining
    # way this can be genuinely unconfigured, and the answer belongs on the page
    # with the button on it. `verifier.enabled` defaults True now, so an install
    # with no agent_cli_reviewers at all would otherwise queue a command whose only
    # output is a line in a log nobody is tailing.
    if resolve_verifier_reviewer(operator_config, verifier) is None:
        have = ", ".join(rc.name for rc in operator_config.agent_cli_reviewers)
        return render(
            request,
            "dashboard/_security_verify_queued.html",
            {
                "report": report,
                "blocked": (
                    f"No agent_cli_reviewers entry named '{verifier.reviewer}', so there "
                    "is no coding-agent CLI to run. "
                    + (
                        f"Configured entries: {have}. Point "
                        "security_triage.verifier.reviewer at one of them."
                        if have
                        else "Add one to operator.yaml — it describes the CLI to invoke "
                        "and, for a remote setup, how to reach the box it runs on."
                    )
                ),
            },
        )
    if report.project is None:
        return render(
            request,
            "dashboard/_security_verify_queued.html",
            {
                "report": report,
                "blocked": (
                    "This report isn't attached to a project, so there's no repository "
                    "to check it against. Set one and try again."
                ),
            },
        )

    created = queue_verification(report, priority=PRIORITY_INTERACTIVE)
    return render(
        request,
        "dashboard/_security_verify_queued.html",
        {"report": report, "created": created, "verifier": verifier},
    )


@require_POST
def security_report_fix(request: HttpRequest, report_id: int) -> HttpResponse:
    """Launch the one-click fix agent (htmx).

    In-request because the launch is one POST to the Cursor API and the
    operator is standing there — the answer (an agent id, or exactly why not)
    is the feedback. The run itself takes minutes on Cursor's infra and nothing
    here waits on it; the branch lands on the fork when it lands.
    """
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.fix_agent import FixAgentError, launch_fix_agent

    try:
        launch_fix_agent(report, get_operator_config())
    except FixAgentError as exc:
        return render(
            request,
            "dashboard/_security_fix_status.html",
            {"report": report, "blocked": str(exc)},
        )
    return render(request, "dashboard/_security_fix_status.html", {"report": report})


@require_POST
def security_report_fix_refresh(request: HttpRequest, report_id: int) -> HttpResponse:
    """Ask the Cursor API and the fork where the fix run got to (htmx)."""
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.fix_agent import refresh_fix_status

    note = refresh_fix_status(report, get_operator_config())
    return render(request, "dashboard/_security_fix_status.html", {"report": report, "note": note})


@require_POST
def security_match_branches(request: HttpRequest) -> HttpResponse:
    """Queue the git sweep that ties a fix branch to every report lacking one.

    Fetches origin and matches CVE ids, scanner finding ids and cited paths
    against every recently-touched branch. Git only — no agent, no model — but
    slow: a fetch plus two ``git log`` calls per branch per project, and there
    can be a few hundred branches. So it is a worker command at bulk priority
    rather than in-request work, and the flash says where to watch for it.
    """
    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.branch_scan import projects_with_open_reports
    from franktheunicorn.security.queue import queue_branch_sweep

    reason = _branch_sweep_gate_reason(get_operator_config())
    if reason:
        messages.error(request, reason)
        return _back_to_security_list(request)
    if not projects_with_open_reports():
        messages.info(
            request,
            "No open report is attached to a project, so there is no repo to look at. "
            "A report needs a project before either git sweep can run.",
        )
        return _back_to_security_list(request)

    if queue_branch_sweep("match_security_branches"):
        messages.success(
            request,
            "Queued the branch sweep: frank will fetch origin and look for the branch "
            "carrying each report's fix. Slow — a few hundred branches per project — and "
            "it runs at bulk priority, so it waits behind nothing but other bulk work. "
            "Confident matches are recorded as the fix branch; the rest show as "
            "suggestions on the reports.",
        )
    else:
        messages.info(request, "A branch sweep is already queued or running.")
    return _back_to_security_list(request)


@require_POST
def security_scan_fixed(request: HttpRequest) -> HttpResponse:
    """Queue the git sweep that reverse-applies proposed patches to find fixed reports.

    The cheap, definitive sibling of :func:`security_recheck_fixed`: where that
    one pays a cloud agent to read a month of commits and form an opinion,
    ``git apply --check -R`` succeeds only when the patch's change is already in
    the tree. It only works on reports that shipped a patch, and the flash says
    so rather than reporting a silent zero.
    """
    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.queue import queue_branch_sweep

    reason = _branch_sweep_gate_reason(get_operator_config())
    if reason:
        messages.error(request, reason)
        return _back_to_security_list(request)
    patchable = (
        SecurityReport.objects.filter(project__isnull=False, status__in=("new", "valid"))
        .filter(fixed_in_branch="")
        .exclude(proposed_patch="")
        .count()
    )
    if not patchable:
        messages.info(
            request,
            "No open report carries a proposed patch, and reverse-applying one is the "
            "whole trick here. Use “Check Untriaged vs Recent Changes” for reports "
            "without a patch — that one reads commits with a cloud agent.",
        )
        return _back_to_security_list(request)

    if queue_branch_sweep("scan_security_fixed"):
        messages.success(
            request,
            f"Queued the already-fixed sweep over {patchable} report(s) with a proposed "
            "patch. It fetches origin and reverse-applies each patch, which is proof "
            "rather than a guess — but it is a checkout per branch, so give it a while.",
        )
    else:
        messages.info(request, "An already-fixed sweep is already queued or running.")
    return _back_to_security_list(request)


@require_POST
def security_recheck_fixed(request: HttpRequest) -> HttpResponse:
    """Launch the batch "did recent commits fix these?" recheck (bulk).

    One cloud agent per project over the untriaged backlog; the launches are
    one POST each and happen here, and the waiting is queued for the worker —
    a recheck run is minutes, which is worker time, not request time.
    """
    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.fix_agent import FixAgentError, cursor_api_key
    from franktheunicorn.security.queue import PRIORITY_INTERACTIVE, queue_recheck_poll
    from franktheunicorn.security.recheck import launch_recheck, untriaged_by_project

    operator_config = get_operator_config()
    config = operator_config.security_triage.fix_agent
    if not config.enabled:
        messages.error(
            request,
            "The fix agent is switched off (security_triage.fix_agent.enabled: false "
            "in operator.yaml), and the recheck rides on it.",
        )
        return _back_to_security_list(request)
    if not cursor_api_key(config):
        messages.error(
            request,
            f"Recheck needs a Cursor API key — set {config.api_key_env} in the environment.",
        )
        return _back_to_security_list(request)

    grouped = untriaged_by_project()
    if not grouped:
        messages.info(request, "No untriaged reports with a project to check.")
        return _back_to_security_list(request)

    # A launched run older than the poll's own timeout outlived its poll
    # without being marked — the worker died or the command was lost. It is
    # not coming back, and leaving it "launched" would block the button for
    # that project forever.
    stale_before = timezone.now() - timedelta(seconds=config.recheck_timeout_seconds)
    launched_runs = SecurityRecheckRun.objects.filter(status="launched")
    stale = launched_runs.filter(created_at__lt=stale_before)
    if stale.exists():
        stale.update(
            status="error",
            detail="the poll never finished it — marked stale by a later recheck press",
            updated_at=timezone.now(),
        )
    in_flight = set(
        launched_runs.filter(created_at__gte=stale_before).values_list("project_id", flat=True)
    )
    launched = 0
    covered = 0
    needs_poll = False
    failures: list[str] = []
    for project, reports in grouped.items():
        if project.pk in in_flight:
            # The running agent's prompt already covers these reports; a second
            # one would answer the same question twice at full price.
            failures.append(f"{project.full_name}: a recheck is already running")
            continue
        try:
            launch_recheck(project, reports, operator_config)
        except FixAgentError as exc:
            # A mid-loop failure can leave earlier chunks launched and billing;
            # those runs still need their poll, and the message shouldn't claim
            # nothing happened.
            partial = SecurityRecheckRun.objects.filter(project=project, status="launched")
            if partial.exists():
                needs_poll = True
                failures.append(
                    f"{project.full_name}: {exc} — {partial.count()} chunk(s) did "
                    "launch and will be polled"
                )
            else:
                failures.append(f"{project.full_name}: {exc}")
        else:
            launched += 1
            covered += len(reports)
    # Any live run needs a poll, not just one this press started. A run whose
    # poll command died — worker restart, budget spent — was otherwise never
    # asked about again: it stayed launched, this branch declined to queue
    # anything because `launched` was 0, and the stale sweep above eventually
    # binned it. Its verdicts were paid for and thrown away every time.
    if launched or needs_poll or SecurityRecheckRun.objects.filter(status="launched").exists():
        queue_recheck_poll(priority=PRIORITY_INTERACTIVE)
    if launched:
        messages.success(
            request,
            f"Recheck launched for {launched} project(s) covering {covered} report(s) — "
            "verdicts land on the reports as the runs finish.",
        )
    for failure in failures:
        messages.error(request, f"Recheck not launched for {failure}.")
    return _back_to_security_list(request)


def _git_scan_blocker(report: SecurityReport, what: str, needs: str) -> str:
    """Why a git-only scan of *report* can't run, or "" if it can.

    Shared by version mapping and introduction dating: both want the verifier's
    checkout and neither wants an agent, so both are stopped by exactly the same
    three things. *what* names the feature and *needs* what the checkout is for,
    since a message that doesn't say which button you pressed is no better than
    a silent no-op.
    """
    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.verifier import resolve_verifier_reviewer

    operator_config = get_operator_config()
    verifier = operator_config.security_triage.verifier
    if not verifier.enabled:
        return (
            f"{what} has been switched off with the rest of verification. "
            "security_triage.verifier.enabled is false in operator.yaml."
        )
    if resolve_verifier_reviewer(operator_config, verifier) is None:
        have = ", ".join(rc.name for rc in operator_config.agent_cli_reviewers)
        return (
            f"No agent_cli_reviewers entry named '{verifier.reviewer}', so there is no "
            f"checkout to {needs}. "
            + (f"Configured entries: {have}." if have else "Add one to operator.yaml.")
        )
    if report.project is None:
        return (
            "This report isn't attached to a project, so there's no repository to "
            f"{needs}. Set one and try again."
        )
    return ""


@require_POST
def security_report_map_versions(request: HttpRequest, report_id: int) -> HttpResponse:
    """Queue cheap version mapping: cited files vs every active release branch (htmx).

    Not the deep verifier. Git ls-tree only, no agent, no branch count cap. The box
    you want on a 143-report archive.
    """
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    from franktheunicorn.security.queue import PRIORITY_INTERACTIVE, queue_version_map

    blocked = _git_scan_blocker(report, "Version mapping", "list branches in")
    if blocked:
        return render(
            request,
            "dashboard/_security_version_map_queued.html",
            {"report": report, "blocked": blocked},
        )

    created = queue_version_map(report, priority=PRIORITY_INTERACTIVE)
    return render(
        request,
        "dashboard/_security_version_map_queued.html",
        {"report": report, "created": created},
    )


@require_POST
def security_report_find_introduction(request: HttpRequest, report_id: int) -> HttpResponse:
    """Queue git-history dating: which commit introduced this, which releases have it.

    The other half of the version question. Version mapping asks whether the cited
    files are *present* on a branch; this asks when the vulnerable code arrived and
    which released tags contain that commit — which is the answer a maintainer
    actually publishes. Git only, no agent.
    """
    report = get_object_or_404(SecurityReport.objects.select_related("project"), pk=report_id)

    from franktheunicorn.security.queue import PRIORITY_INTERACTIVE, queue_introduction_scan

    blocked = _git_scan_blocker(report, "Introduction dating", "search history in")
    if blocked:
        return render(
            request,
            "dashboard/_security_introduction_queued.html",
            {"report": report, "blocked": blocked},
        )

    created = queue_introduction_scan(report, priority=PRIORITY_INTERACTIVE)
    return render(
        request,
        "dashboard/_security_introduction_queued.html",
        {"report": report, "created": created},
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
        return HttpResponse('<div class="cve-result error-note">CVE lookup failed.</div>')

    return render(request, "dashboard/_security_cve_matches.html", {"report": report})


def security_guidance_list(request: HttpRequest) -> HttpResponse:
    """Overview of learned triage guidance, per project and global.

    Mirrors ``anti_pattern_list`` — a view of what the iterative learning loop
    has distilled so far, plus the raw material waiting for the next
    distillation (feedback rows and the operator's own rulings), and the button
    that triggers it.
    """
    from franktheunicorn.security.learning import RULED_STATUSES

    guidance = SecurityTriageGuidance.objects.select_related("project").filter(is_active=True)
    return render(
        request,
        "dashboard/security_guidance.html",
        {
            "guidance_rows": guidance,
            "feedback_count": SecurityTriageFeedback.objects.count(),
            "rulings_count": SecurityReport.objects.filter(status__in=RULED_STATUSES).count(),
        },
    )


@require_POST
def security_guidance_distill(request: HttpRequest) -> HttpResponse:
    """Distill accumulated feedback and operator rulings into guidance, on demand.

    Explicit agree/disagree feedback distills itself as it's recorded; the loop's
    other inputs — verdict saves that overrode (or matched) a staged suggestion,
    the Agree button, rulings on never-triaged reports — only record, so without
    this button nothing turns them into guidance until the next feedback click.
    One LLM call per scope (global, plus each project with anything to learn
    from), in-request: no container, no queue.
    """
    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.security.learning import RULED_STATUSES, distill_triage_guidance

    operator_config = get_operator_config()
    if not operator_config.llm_backends:
        messages.error(request, "No LLM backend configured. Add one to operator.yaml.")
        return _back_to_guidance(request)

    projects = list(
        Project.objects.filter(
            Q(triage_feedback__isnull=False) | Q(security_reports__status__in=RULED_STATUSES)
        ).distinct()
    )

    distilled = 0
    if distill_triage_guidance(None, operator_config) is not None:
        distilled += 1
    for project in projects:
        if distill_triage_guidance(project, operator_config) is not None:
            distilled += 1

    if distilled:
        messages.success(
            request,
            f"Distilled guidance for {distilled} scope(s) "
            f"(global plus {len(projects)} project(s) with feedback or rulings).",
        )
    else:
        messages.warning(
            request,
            "Nothing distilled — either there is no feedback and no operator ruling "
            "to learn from yet, or the LLM call failed (see the log).",
        )
    logger.info(
        "Guidance distillation on demand: %d scope(s) distilled across %d project(s)",
        distilled,
        len(projects),
    )
    return _back_to_guidance(request)


def _back_to_guidance(request: HttpRequest) -> HttpResponse:
    target = reverse("dashboard:security_guidance")
    if request.headers.get("HX-Request") == "true":
        return HttpResponse(status=204, headers={"HX-Redirect": target})
    return redirect(target)


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
            '<div class="run-agents-result error-note">No project config found for this repo.</div>'
        )
    # Enqueue rather than run inline: process_pr makes external HTTP +
    # LLM calls and can take 30-120s, which would tie up the web request.
    #
    # Through security.queue, not straight to WorkerCommand — that is where the
    # in-flight dedup and the priority live. Without them this button queued a
    # second full pipeline run per impatient click, all of them behind whatever
    # bulk work the queue already held.
    from franktheunicorn.security.queue import PRIORITY_INTERACTIVE, queue_command

    created = queue_command("run_agents", pull_request=pr, priority=PRIORITY_INTERACTIVE)
    if not created:
        # Says so instead of silently queueing nothing. The old button gave the
        # same "queued" message either way, so a run already in flight looked
        # identical to a fresh one and the honest answer — "it is already going" —
        # was never shown.
        return HttpResponse(
            '<div class="run-agents-result queued-note">'
            "An agent run for this PR is already queued or running — "
            "reload to see it land.</div>"
        )
    return HttpResponse(
        '<div class="run-agents-result queued-note">'
        "Agent run queued at the front of the worker queue. "
        "Reload this page in a minute or two to see updated findings.</div>"
    )


@require_POST
def regenerate_findings(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Wipe stale drafts (keep rejected) and queue a fresh agent run (htmx).

    Refuses to wipe while a run is already *running* — that run may have
    already written drafts, and the in-flight dedup would not queue a
    replacement. A *pending* run is fine: wipe first, it hasn't started.
    """
    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)
    from franktheunicorn.config.loader import get_project_config

    project_config = get_project_config(pr.project.full_name)
    if not project_config:
        return HttpResponse(
            '<div class="run-agents-result error-note">No project config found for this repo.</div>'
        )

    from franktheunicorn.security.queue import PRIORITY_INTERACTIVE, queue_command

    running = WorkerCommand.objects.filter(
        command="run_agents", pull_request=pr, status="running"
    ).exists()
    if running:
        return HttpResponse(
            '<div class="run-agents-result queued-note">'
            "An agent run for this PR is already in progress — "
            "wait for it to finish before regenerating, or you would "
            "wipe findings it just wrote and get no replacement run.</div>"
        )

    kept = ReviewDraft.objects.filter(pull_request=pr, status="rejected").count()
    deleted = ReviewDraft.wipe_for_regenerate(pr)
    vibe_deleted, _ = AgentVibe.objects.filter(pull_request=pr).delete()
    logger.info(
        "Regenerate: deleted %d non-rejected draft(s) and %d vibe(s) on %s #%s; "
        "rejected drafts kept",
        deleted,
        vibe_deleted,
        pr.project.full_name,
        pr.number,
    )

    created = queue_command("run_agents", pull_request=pr, priority=PRIORITY_INTERACTIVE)
    kept_note = f" Kept {kept} rejected." if kept else ""
    follow = (
        "An agent run for this PR is already queued — reload to see it land."
        if not created
        else (
            "Agent run queued at the front of the worker queue. "
            "Reload this page in a minute or two to see updated findings."
        )
    )
    return HttpResponse(
        f'<div class="run-agents-result queued-note">Deleted {deleted} finding(s).{kept_note} {follow}</div>'
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
            '<div class="run-tests-result error-note">No project config found for this repo.</div>'
        )
    if not project_config.tests.enabled:
        return HttpResponse(
            '<div class="run-tests-result error-note">'
            "Differential tests are not enabled for this project. "
            "Add <code>tests: enabled: true</code> to the project YAML.</div>"
        )

    # Same door as the other buttons: dedup so a double-click is one container
    # run, and interactive priority so it does not wait behind bulk work.
    from franktheunicorn.security.queue import PRIORITY_INTERACTIVE, queue_command

    if not queue_command("run_dual_tests", pull_request=pr, priority=PRIORITY_INTERACTIVE):
        return HttpResponse(
            '<div class="run-tests-result queued-note">'
            "A test run for this PR is already queued or running.</div>"
        )
    return HttpResponse(
        '<div class="run-tests-result queued-note">'
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
