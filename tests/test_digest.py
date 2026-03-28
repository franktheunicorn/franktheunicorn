"""Tests for the digest module."""

from __future__ import annotations

import datetime

from franktheunicorn.config import ProjectConfig
from franktheunicorn.digest import build_digest, send_digest
from franktheunicorn.github_client import GitHubPR
from franktheunicorn.scoring import ScoreResult
from franktheunicorn.storage import upsert_project, upsert_pull_request


def _seed_prs(db_session, scores: list[float]) -> None:
    cfg = ProjectConfig(slug="digest-test", repo="owner/repo")
    project = upsert_project(db_session, cfg)
    db_session.flush()
    now = datetime.datetime.now(datetime.UTC)

    for i, score in enumerate(scores):
        github_pr = GitHubPR(
            {
                "number": 200 + i,
                "title": f"PR {i}",
                "user": {"login": "alice"},
                "state": "open",
                "html_url": f"https://github.com/owner/repo/pull/{200 + i}",
                "body": "A reasonable description that passes the AI heuristic check.",
                "labels": [],
                "requested_reviewers": [],
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }
        )
        sr = ScoreResult()
        sr.score = score
        upsert_pull_request(db_session, project, github_pr, sr, [])
    db_session.flush()


def test_build_digest_empty(db_session):
    digest = build_digest(db_session)
    assert digest["total_recent_prs"] == 0
    assert digest["high_priority"] == []
    assert digest["medium_priority"] == []


def test_build_digest_with_prs(db_session):
    _seed_prs(db_session, [1.5, 0.6, 0.2])
    digest = build_digest(db_session, since_hours=1)
    assert digest["total_recent_prs"] == 3
    assert len(digest["high_priority"]) == 1
    assert len(digest["medium_priority"]) == 1


def test_send_digest_runs(db_session, caplog):
    _seed_prs(db_session, [1.2, 0.7])
    digest = build_digest(db_session, since_hours=1)
    import logging

    with caplog.at_level(logging.INFO, logger="franktheunicorn.digest"):
        send_digest(digest)
    assert "Digest" in caplog.text
