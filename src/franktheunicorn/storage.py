"""Storage helpers: CRUD operations on the ORM models.

Keep this layer thin - it is just a convenience wrapper around SQLAlchemy
sessions so the worker and web service do not have to repeat boilerplate.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from franktheunicorn.config import ProjectConfig
from franktheunicorn.github_client import GitHubPR
from franktheunicorn.models import AntiPattern, OperatorAction, Project, PullRequest, ReviewDraft
from franktheunicorn.scoring import ScoreResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


def upsert_project(session: Session, config: ProjectConfig) -> Project:
    """Insert or update a Project row from a ProjectConfig."""
    project = session.query(Project).filter(Project.slug == config.slug).first()
    if project is None:
        project = Project(slug=config.slug)
        session.add(project)
    project.repo = config.repo
    project.review_context = config.review_context
    project.asf_project = config.asf_project
    project.enabled = config.enabled
    return project


def get_project_by_slug(session: Session, slug: str) -> Project | None:
    return session.query(Project).filter(Project.slug == slug).first()


def list_projects(session: Session) -> list[Project]:
    return session.query(Project).filter(Project.enabled.is_(True)).all()


# ---------------------------------------------------------------------------
# Pull Request
# ---------------------------------------------------------------------------


def upsert_pull_request(
    session: Session,
    project: Project,
    github_pr: GitHubPR,
    score_result: ScoreResult,
    changed_files: list[str],
) -> PullRequest:
    """Insert or update a PullRequest row."""
    pr = (
        session.query(PullRequest)
        .filter(
            PullRequest.project_id == project.id,
            PullRequest.github_pr_number == github_pr.number,
        )
        .first()
    )
    if pr is None:
        pr = PullRequest(project_id=project.id, github_pr_number=github_pr.number)
        session.add(pr)

    pr.title = github_pr.title
    pr.author_login = github_pr.author_login
    pr.state = github_pr.state
    pr.html_url = github_pr.html_url
    pr.body = github_pr.body
    pr.labels = ",".join(github_pr.labels)
    pr.changed_files_json = json.dumps(changed_files)
    pr.interest_score = score_result.score
    pr.operator_is_author = score_result.operator_is_author
    pr.operator_mentioned = score_result.operator_mentioned
    pr.likely_ai_generated = score_result.likely_ai_generated
    pr.new_contributor = score_result.new_contributor
    pr.github_created_at = github_pr.created_at
    pr.github_updated_at = github_pr.updated_at

    import datetime

    pr.last_scored_at = datetime.datetime.now(datetime.UTC)
    return pr


def list_pull_requests(
    session: Session,
    project_id: int | None = None,
    state: str = "open",
    limit: int = 100,
    order_by_score: bool = True,
) -> list[PullRequest]:
    """List pull requests, optionally filtered by project and state."""
    q = session.query(PullRequest)
    if project_id is not None:
        q = q.filter(PullRequest.project_id == project_id)
    if state:
        q = q.filter(PullRequest.state == state)
    if order_by_score:
        q = q.order_by(PullRequest.interest_score.desc())
    return q.limit(limit).all()


def get_pull_request(session: Session, pr_id: int) -> PullRequest | None:
    return session.query(PullRequest).filter(PullRequest.id == pr_id).first()


# ---------------------------------------------------------------------------
# Review Draft
# ---------------------------------------------------------------------------


def save_review_draft(session: Session, draft: ReviewDraft) -> ReviewDraft:
    session.add(draft)
    return draft


def list_drafts_for_pr(session: Session, pr_id: int) -> list[ReviewDraft]:
    return (
        session.query(ReviewDraft)
        .filter(ReviewDraft.pull_request_id == pr_id, ReviewDraft.status == "pending")
        .all()
    )


# ---------------------------------------------------------------------------
# Operator Action
# ---------------------------------------------------------------------------


def record_operator_action(
    session: Session,
    pr_id: int,
    action: str,
    note: str = "",
) -> OperatorAction:
    """Record an operator action (posted, discarded, snoozed, etc.)."""
    oa = OperatorAction(pull_request_id=pr_id, action=action, note=note)
    session.add(oa)
    return oa


# ---------------------------------------------------------------------------
# Anti-Pattern (convenience re-export)
# ---------------------------------------------------------------------------


def list_anti_patterns(session: Session) -> list[AntiPattern]:
    return session.query(AntiPattern).order_by(AntiPattern.probability.desc()).all()
