"""FastAPI web dashboard for franktheunicorn.

Lightweight, operator-only.  No auth, no multi-tenancy.
Local-first: the SQLite DB is on the local filesystem.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from franktheunicorn.database import create_all_tables, get_db
from franktheunicorn.models import PullRequest
from franktheunicorn.storage import (
    list_anti_patterns,
    list_projects,
    list_pull_requests,
    record_operator_action,
)

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Create tables on startup."""
    create_all_tables()
    logger.info("franktheunicorn web service started")
    yield


app = FastAPI(
    title="franktheunicorn",
    description="Local-first AI code review assistant",
    version="0.1.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """Main dashboard showing ingested PRs ordered by interest score."""
    with get_db() as session:
        projects = list_projects(session)
        prs = list_pull_requests(session, state="open", limit=100, order_by_score=True)

        # Attach project slug to each PR for display.
        project_map = {p.id: p.slug for p in projects}
        pr_rows = []
        for pr in prs:
            pr_rows.append(
                {
                    "id": pr.id,
                    "project": project_map.get(pr.project_id, "unknown"),
                    "number": pr.github_pr_number,
                    "title": pr.title,
                    "author": pr.author_login,
                    "score": round(pr.interest_score, 2),
                    "url": pr.html_url,
                    "labels": pr.labels,
                    "operator_is_author": pr.operator_is_author,
                    "operator_mentioned": pr.operator_mentioned,
                    "new_contributor": pr.new_contributor,
                    "likely_ai": pr.likely_ai_generated,
                }
            )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "prs": pr_rows,
            "total": len(pr_rows),
        },
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@app.get("/api/prs", response_class=JSONResponse)
def api_list_prs(
    project_id: int | None = None,
    state: str = "open",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return PR list as JSON."""
    with get_db() as session:
        prs: list[PullRequest] = list_pull_requests(
            session,
            project_id=project_id,
            state=state,
            limit=limit,
            order_by_score=True,
        )
        return [
            {
                "id": pr.id,
                "project_id": pr.project_id,
                "github_pr_number": pr.github_pr_number,
                "title": pr.title,
                "author_login": pr.author_login,
                "state": pr.state,
                "html_url": pr.html_url,
                "interest_score": pr.interest_score,
                "operator_is_author": pr.operator_is_author,
                "operator_mentioned": pr.operator_mentioned,
                "new_contributor": pr.new_contributor,
                "likely_ai_generated": pr.likely_ai_generated,
                "labels": pr.labels,
                "changed_files": json.loads(pr.changed_files_json or "[]"),
            }
            for pr in prs
        ]


@app.get("/api/projects", response_class=JSONResponse)
def api_list_projects() -> list[dict[str, Any]]:
    """Return project list as JSON."""
    with get_db() as session:
        projects = list_projects(session)
        return [
            {
                "id": p.id,
                "slug": p.slug,
                "repo": p.repo,
                "asf_project": p.asf_project,
                "enabled": p.enabled,
            }
            for p in projects
        ]


@app.get("/api/anti-patterns", response_class=JSONResponse)
def api_list_anti_patterns() -> list[dict[str, Any]]:
    """Return stored anti-patterns as JSON."""
    with get_db() as session:
        aps = list_anti_patterns(session)
        return [
            {
                "id": ap.id,
                "label": ap.label,
                "phrase": ap.phrase,
                "count": ap.count,
                "total_shown": ap.total_shown,
                "probability": ap.probability,
                "project_slug": ap.project_slug,
            }
            for ap in aps
        ]


@app.post("/api/prs/{pr_id}/action", response_class=JSONResponse)
def api_pr_action(pr_id: int, action: str, note: str = "") -> dict[str, Any]:
    """Record an operator action on a PR (posted, discarded, snoozed, etc.)."""
    valid_actions = {"posted", "discarded", "edited", "snoozed", "flagged-anti-pattern"}
    if action not in valid_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action. Must be one of: {', '.join(sorted(valid_actions))}",
        )
    with get_db() as session:
        oa = record_operator_action(session, pr_id=pr_id, action=action, note=note)
        return {"ok": True, "action_id": oa.id}


@app.get("/healthz")
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
