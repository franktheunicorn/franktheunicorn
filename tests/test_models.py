"""Tests for ORM models."""

from __future__ import annotations

from franktheunicorn.models import AntiPattern, OperatorAction, Project, PullRequest, ReviewDraft


def test_project_repr(db_session):
    p = Project(slug="test", repo="owner/repo")
    db_session.add(p)
    db_session.flush()
    assert "test" in repr(p)


def test_pull_request_repr(db_session):
    p = Project(slug="test", repo="owner/repo")
    db_session.add(p)
    db_session.flush()

    pr = PullRequest(
        project_id=p.id,
        github_pr_number=42,
        title="Fix bug",
        author_login="alice",
        html_url="https://github.com/owner/repo/pull/42",
    )
    db_session.add(pr)
    db_session.flush()
    assert "42" in repr(pr)


def test_review_draft_repr(db_session):
    p = Project(slug="test", repo="owner/repo")
    db_session.add(p)
    db_session.flush()
    pr = PullRequest(
        project_id=p.id,
        github_pr_number=1,
        title="T",
        author_login="u",
        html_url="https://github.com/x/y/pull/1",
    )
    db_session.add(pr)
    db_session.flush()
    draft = ReviewDraft(pull_request_id=pr.id, body="Hello")
    db_session.add(draft)
    db_session.flush()
    assert "pending" in repr(draft)


def test_anti_pattern_repr(db_session):
    ap = AntiPattern(label="nitpick", phrase="please fix this", probability=0.9)
    db_session.add(ap)
    db_session.flush()
    assert "nitpick" in repr(ap)


def test_operator_action_repr(db_session):
    p = Project(slug="test", repo="owner/repo")
    db_session.add(p)
    db_session.flush()
    pr = PullRequest(
        project_id=p.id,
        github_pr_number=1,
        title="T",
        author_login="u",
        html_url="https://github.com/x/y/pull/1",
    )
    db_session.add(pr)
    db_session.flush()
    oa = OperatorAction(pull_request_id=pr.id, action="posted")
    db_session.add(oa)
    db_session.flush()
    assert "posted" in repr(oa)


def test_project_cascade_delete(db_session):
    p = Project(slug="cascade-test", repo="owner/repo")
    db_session.add(p)
    db_session.flush()

    pr = PullRequest(
        project_id=p.id,
        github_pr_number=99,
        title="Cascade",
        author_login="dave",
        html_url="https://github.com/owner/repo/pull/99",
    )
    db_session.add(pr)
    db_session.flush()

    db_session.delete(p)
    db_session.flush()

    remaining = db_session.query(PullRequest).filter(PullRequest.github_pr_number == 99).first()
    assert remaining is None


def test_pull_request_defaults(db_session):
    p = Project(slug="defaults", repo="owner/repo")
    db_session.add(p)
    db_session.flush()
    pr = PullRequest(
        project_id=p.id,
        github_pr_number=1,
        title="T",
        author_login="u",
        html_url="https://github.com/x/y/pull/1",
    )
    db_session.add(pr)
    db_session.flush()
    assert pr.state == "open"
    assert pr.interest_score == 0.0
    assert pr.operator_is_author is False
    assert pr.likely_ai_generated is False
