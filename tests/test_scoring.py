"""Tests for the interest scoring module."""

from __future__ import annotations

import datetime

from franktheunicorn.config import OperatorConfig, ProjectConfig
from franktheunicorn.scoring import ScoringContext, _is_likely_ai_generated, score_pr


def _make_ctx(
    author_login: str = "stranger",
    body: str = (
        "Refactors the connection pool to reduce overhead under high load. "
        "Benchmarks show a 30% reduction in p99 latency for bulk operations. "
        "Tests updated accordingly."
    ),
    labels: list[str] | None = None,
    requested_reviewers: list[str] | None = None,
    changed_files: list[str] | None = None,
    updated_at: datetime.datetime | None = None,
    operator: OperatorConfig | None = None,
    project: ProjectConfig | None = None,
    mentions: list[str] | None = None,
) -> ScoringContext:
    return ScoringContext(
        author_login=author_login,
        body=body,
        labels=labels or [],
        requested_reviewers=requested_reviewers or [],
        changed_files=changed_files or [],
        updated_at=updated_at,
        operator=operator
        or OperatorConfig(
            github_login="franktheunicorn",
            trusted_collaborators=["alice", "bob"],
        ),
        project=project
        or ProjectConfig(
            slug="myproject",
            repo="example/myproject",
            watched_paths=["src/core/**"],
            frequent_contributors=["carol"],
        ),
        mentions_in_comments=mentions or [],
    )


def test_operator_is_author():
    ctx = _make_ctx(author_login="franktheunicorn")
    result = score_pr(ctx)
    assert result.operator_is_author is True
    assert result.score >= 1.0


def test_operator_mentioned_as_reviewer():
    ctx = _make_ctx(requested_reviewers=["franktheunicorn"])
    result = score_pr(ctx)
    assert result.operator_mentioned is True
    assert result.score >= 0.8


def test_operator_mentioned_in_comments():
    ctx = _make_ctx(mentions=["franktheunicorn"])
    result = score_pr(ctx)
    assert result.operator_mentioned is True


def test_watched_path_overlap():
    ctx = _make_ctx(changed_files=["src/core/engine.py"])
    result = score_pr(ctx)
    signals = " ".join(result.signals)
    assert "watched" in signals


def test_frequent_contributor():
    ctx = _make_ctx(author_login="carol")
    result = score_pr(ctx)
    signals = " ".join(result.signals)
    assert "frequent" in signals or "contributor" in signals


def test_trusted_collaborator():
    ctx = _make_ctx(author_login="alice")
    result = score_pr(ctx)
    signals = " ".join(result.signals)
    assert "frequent" in signals or "contributor" in signals


def test_new_contributor():
    ctx = _make_ctx(author_login="totally-new-person")
    result = score_pr(ctx)
    assert result.new_contributor is True
    signals = " ".join(result.signals)
    assert "new contributor" in signals


def test_likely_ai_generated_short_body():
    ctx = _make_ctx(body="Fix bug.")
    result = score_pr(ctx)
    assert result.likely_ai_generated is True


def test_likely_ai_generated_phrase():
    ctx = _make_ctx(
        body="I hope this PR helps you. Please let me know if you have any questions "
        "about the implementation details in this change.",
    )
    result = score_pr(ctx)
    assert result.likely_ai_generated is True


def test_stale_pr_penalty():
    old_date = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=60)
    ctx = _make_ctx(author_login="carol", updated_at=old_date)
    result_stale = score_pr(ctx)

    ctx_fresh = _make_ctx(author_login="carol")
    result_fresh = score_pr(ctx_fresh)

    assert result_stale.score < result_fresh.score


def test_score_floor_zero():
    """Score should never go below 0."""
    ctx = _make_ctx(
        author_login="ai-bot",
        body="I hope this helps!",
    )
    result = score_pr(ctx)
    assert result.score >= 0.0


def test_multiple_signals_accumulate():
    ctx = _make_ctx(
        author_login="franktheunicorn",
        requested_reviewers=["franktheunicorn"],
        changed_files=["src/core/engine.py"],
    )
    result = score_pr(ctx)
    assert result.score >= 2.0


def test_is_likely_ai_generated_long_clean_body():
    body = (
        "This PR refactors the database layer to use connection pooling. "
        "The primary motivation is to reduce connection overhead under load. "
        "Benchmarks show a 30% reduction in p99 latency for bulk operations."
    )
    assert not _is_likely_ai_generated(body)
