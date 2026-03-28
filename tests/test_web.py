"""Tests for the web dashboard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from franktheunicorn.database import create_all_tables, get_db
from web.main import app


@pytest.fixture()
def client(isolated_settings) -> TestClient:
    create_all_tables()
    return TestClient(app)


def test_health_check(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_dashboard_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "franktheunicorn" in resp.text


def test_api_prs_empty(client):
    resp = client.get("/api/prs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_projects_empty(client):
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_anti_patterns_empty(client):
    resp = client.get("/api/anti-patterns")
    assert resp.status_code == 200
    assert resp.json() == []


def _seed_pr(settings) -> int:
    """Seed a single PR and return its ID."""
    from franktheunicorn.config import ProjectConfig
    from franktheunicorn.github_client import GitHubPR
    from franktheunicorn.scoring import ScoreResult
    from franktheunicorn.storage import upsert_project, upsert_pull_request

    with get_db() as session:
        cfg = ProjectConfig(slug="web-test", repo="owner/repo")
        project = upsert_project(session, cfg)
        session.flush()

        github_pr = GitHubPR(
            {
                "number": 7,
                "title": "Dashboard test PR",
                "user": {"login": "dave"},
                "state": "open",
                "html_url": "https://github.com/owner/repo/pull/7",
                "body": "A reasonable description of this PR's purpose and motivation.",
                "labels": [{"name": "feature"}],
                "requested_reviewers": [],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-06-01T00:00:00Z",
            }
        )
        score = ScoreResult()
        score.score = 1.2
        pr = upsert_pull_request(session, project, github_pr, score, ["src/main.py"])
        session.flush()
        return pr.id


def test_dashboard_shows_prs(client, isolated_settings):
    _seed_pr(isolated_settings)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Dashboard test PR" in resp.text


def test_api_prs_shows_seeded_pr(client, isolated_settings):
    _seed_pr(isolated_settings)
    resp = client.get("/api/prs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "Dashboard test PR"
    assert data[0]["interest_score"] == 1.2


def test_api_pr_action_valid(client, isolated_settings):
    pr_id = _seed_pr(isolated_settings)
    resp = client.post(f"/api/prs/{pr_id}/action?action=posted&note=looks+good")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_api_pr_action_invalid(client, isolated_settings):
    pr_id = _seed_pr(isolated_settings)
    resp = client.post(f"/api/prs/{pr_id}/action?action=invalid-action")
    assert resp.status_code == 400


def test_api_projects_after_seed(client, isolated_settings):
    _seed_pr(isolated_settings)
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["slug"] == "web-test"
