"""
Dashboard views — server-rendered HTML. No SPA.

Lightweight pages showing ingested PRs, their interest scores,
and any draft review comments.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render

from franktheunicorn.core.models import PullRequest, ReviewDraft


def index(request: HttpRequest) -> HttpResponse:
    """Main dashboard: list of PRs sorted by interest score."""
    prs = (
        PullRequest.objects.select_related("project")
        .filter(state="open")
        .order_by("-interest_score", "-github_updated_at")[:100]
    )
    return render(request, "dashboard/pr_list.html", {"pull_requests": prs})


def pr_detail(request: HttpRequest, pr_id: int) -> HttpResponse:
    """Detail view for a single PR showing drafts and score breakdown."""
    pr = get_object_or_404(PullRequest.objects.select_related("project"), pk=pr_id)
    drafts = ReviewDraft.objects.filter(pull_request=pr).order_by("-created_at")
    return render(request, "dashboard/pr_detail.html", {"pr": pr, "drafts": drafts})
