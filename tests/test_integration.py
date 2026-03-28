"""Integration-style tests: full polling/scoring/storage flow."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from franktheunicorn.config import OperatorConfig, ProjectConfig
from franktheunicorn.database import create_all_tables, get_db
from franktheunicorn.github_client import GitHubPR
from franktheunicorn.models import PullRequest
from franktheunicorn.poller import get_stored_changed_files, poll_project
from franktheunicorn.scoring import ScoringContext, score_pr
from franktheunicorn.storage import list_pull_requests, upsert_project, upsert_pull_request

FAKE_PR_DATA = {
    "number": 42,
    "title": "Refactor database layer",
    "user": {"login": "carol"},
    "state": "open",
    "html_url": "https://github.com/example/myproject/pull/42",
    "body": (
        "This PR refactors the database layer to use connection pooling. "
        "Benchmarks show a 30% reduction in p99 latency."
    ),
    "labels": [{"name": "refactor"}],
    "requested_reviewers": [{"login": "franktheunicorn"}],
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-06-01T00:00:00Z",
}


@pytest.fixture()
def operator() -> OperatorConfig:
    return OperatorConfig(
        github_login="franktheunicorn",
        trusted_collaborators=["alice"],
        stale_pr_days=30,
    )


@pytest.fixture()
def project_cfg() -> ProjectConfig:
    return ProjectConfig(
        slug="myproject",
        repo="example/myproject",
        watched_paths=["src/db/**"],
        frequent_contributors=["carol"],
        poll_interval_seconds=300,
        max_prs_per_poll=50,
    )


def test_upsert_and_score_pr(db_session, operator, project_cfg):
    """Test that a PR can be inserted, scored, and retrieved."""
    project = upsert_project(db_session, project_cfg)
    db_session.flush()

    github_pr = GitHubPR(FAKE_PR_DATA)
    changed_files = ["src/db/pool.py", "tests/test_pool.py"]

    ctx = ScoringContext(
        author_login=github_pr.author_login,
        body=github_pr.body,
        labels=github_pr.labels,
        requested_reviewers=github_pr.requested_reviewers,
        changed_files=changed_files,
        updated_at=github_pr.updated_at,
        operator=operator,
        project=project_cfg,
        mentions_in_comments=[],
    )
    score_result = score_pr(ctx)

    pr = upsert_pull_request(db_session, project, github_pr, score_result, changed_files)
    db_session.flush()

    # Score should be high: frequent contributor + mentioned + watched path.
    assert pr.interest_score >= 1.0
    assert pr.operator_mentioned is True
    assert pr.author_login == "carol"
    assert pr.github_pr_number == 42
    assert pr.title == "Refactor database layer"


def test_upsert_is_idempotent(db_session, operator, project_cfg):
    """Upserting the same PR twice should not create duplicates."""
    project = upsert_project(db_session, project_cfg)
    db_session.flush()

    github_pr = GitHubPR(FAKE_PR_DATA)
    changed_files: list[str] = []

    ctx = ScoringContext(
        author_login=github_pr.author_login,
        body=github_pr.body,
        labels=[],
        requested_reviewers=[],
        changed_files=changed_files,
        updated_at=github_pr.updated_at,
        operator=operator,
        project=project_cfg,
        mentions_in_comments=[],
    )
    score_result = score_pr(ctx)

    upsert_pull_request(db_session, project, github_pr, score_result, changed_files)
    db_session.flush()
    upsert_pull_request(db_session, project, github_pr, score_result, changed_files)
    db_session.flush()

    count = db_session.query(PullRequest).filter(PullRequest.github_pr_number == 42).count()
    assert count == 1


def test_list_pull_requests_ordered_by_score(db_session, operator, project_cfg):
    """PRs should be returned ordered by interest_score descending."""
    project = upsert_project(db_session, project_cfg)
    db_session.flush()

    for i, score in enumerate([0.3, 1.5, 0.7]):
        data = dict(FAKE_PR_DATA, number=100 + i)
        github_pr = GitHubPR(data)
        ctx = ScoringContext(
            author_login=github_pr.author_login,
            body=github_pr.body,
            labels=[],
            requested_reviewers=[],
            changed_files=[],
            updated_at=github_pr.updated_at,
            operator=operator,
            project=project_cfg,
            mentions_in_comments=[],
        )
        score_result = score_pr(ctx)
        score_result.score = score  # override for determinism
        upsert_pull_request(db_session, project, github_pr, score_result, [])
    db_session.flush()

    prs = list_pull_requests(db_session, order_by_score=True)
    scores = [pr.interest_score for pr in prs]
    assert scores == sorted(scores, reverse=True)


def test_poll_project_integration(isolated_settings, operator, project_cfg):
    """Full poll_project flow with mocked GitHub client."""
    create_all_tables()

    mock_client = MagicMock()
    mock_client.list_open_prs.return_value = [GitHubPR(FAKE_PR_DATA)]
    mock_client.list_pr_files.return_value = ["src/db/pool.py"]
    mock_client.get_issue_comments.return_value = [
        {"user": {"login": "franktheunicorn"}, "body": "PTAL"}
    ]

    count = poll_project(mock_client, project_cfg, operator)
    assert count == 1

    with get_db() as session:
        prs = list_pull_requests(session)
        assert len(prs) == 1
        assert prs[0].github_pr_number == 42


def test_poll_project_empty_response(isolated_settings, operator, project_cfg):
    """poll_project returns 0 if no PRs found."""
    create_all_tables()

    mock_client = MagicMock()
    mock_client.list_open_prs.return_value = []

    count = poll_project(mock_client, project_cfg, operator)
    assert count == 0


def test_get_stored_changed_files():
    files = get_stored_changed_files('["a.py", "b.py"]')
    assert files == ["a.py", "b.py"]


def test_get_stored_changed_files_empty():
    assert get_stored_changed_files("[]") == []
    assert get_stored_changed_files("") == []
    assert get_stored_changed_files("not-json") == []
