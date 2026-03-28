"""Tests for the storage module."""

from __future__ import annotations

from franktheunicorn.config import ProjectConfig
from franktheunicorn.github_client import GitHubPR
from franktheunicorn.models import Project
from franktheunicorn.scoring import ScoreResult
from franktheunicorn.storage import (
    get_project_by_slug,
    get_pull_request,
    list_anti_patterns,
    list_pull_requests,
    record_operator_action,
    upsert_project,
    upsert_pull_request,
)


def _make_project_config(slug: str = "test", repo: str = "owner/repo") -> ProjectConfig:
    return ProjectConfig(slug=slug, repo=repo)


def _make_github_pr(number: int = 1) -> GitHubPR:
    return GitHubPR(
        {
            "number": number,
            "title": f"PR #{number}",
            "user": {"login": "alice"},
            "state": "open",
            "html_url": f"https://github.com/owner/repo/pull/{number}",
            "body": "Some description that is long enough to pass the AI heuristic.",
            "labels": [],
            "requested_reviewers": [],
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-06-01T00:00:00Z",
        }
    )


def _make_score_result(score: float = 0.5) -> ScoreResult:
    r = ScoreResult()
    r.score = score
    return r


def test_upsert_project_creates(db_session):
    cfg = _make_project_config()
    project = upsert_project(db_session, cfg)
    db_session.flush()
    assert project.slug == "test"
    assert project.repo == "owner/repo"


def test_upsert_project_updates(db_session):
    cfg = _make_project_config()
    upsert_project(db_session, cfg)
    db_session.flush()

    cfg2 = _make_project_config(repo="owner/new-repo")
    upsert_project(db_session, cfg2)
    db_session.flush()

    project = get_project_by_slug(db_session, "test")
    assert project is not None
    assert project.repo == "owner/new-repo"
    count = db_session.query(Project).count()
    assert count == 1


def test_get_project_by_slug_missing(db_session):
    assert get_project_by_slug(db_session, "nonexistent") is None


def test_upsert_pull_request(db_session):
    cfg = _make_project_config()
    project = upsert_project(db_session, cfg)
    db_session.flush()

    github_pr = _make_github_pr(number=5)
    score = _make_score_result(score=0.8)
    pr = upsert_pull_request(db_session, project, github_pr, score, ["src/main.py"])
    db_session.flush()

    assert pr.github_pr_number == 5
    assert pr.interest_score == 0.8
    assert "src/main.py" in pr.changed_files_json


def test_get_pull_request(db_session):
    cfg = _make_project_config()
    project = upsert_project(db_session, cfg)
    db_session.flush()

    github_pr = _make_github_pr(number=7)
    score = _make_score_result()
    pr = upsert_pull_request(db_session, project, github_pr, score, [])
    db_session.flush()

    fetched = get_pull_request(db_session, pr.id)
    assert fetched is not None
    assert fetched.github_pr_number == 7


def test_get_pull_request_missing(db_session):
    assert get_pull_request(db_session, 999999) is None


def test_list_pull_requests_empty(db_session):
    prs = list_pull_requests(db_session)
    assert prs == []


def test_record_operator_action(db_session):
    cfg = _make_project_config()
    project = upsert_project(db_session, cfg)
    db_session.flush()

    github_pr = _make_github_pr()
    score = _make_score_result()
    pr = upsert_pull_request(db_session, project, github_pr, score, [])
    db_session.flush()

    oa = record_operator_action(db_session, pr_id=pr.id, action="posted", note="good one")
    db_session.flush()

    assert oa.action == "posted"
    assert oa.note == "good one"
    assert oa.pull_request_id == pr.id


def test_list_anti_patterns_empty(db_session):
    patterns = list_anti_patterns(db_session)
    assert patterns == []
