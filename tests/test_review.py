"""Tests for the stub review drafting service."""

from __future__ import annotations

import pytest

from franktheunicorn.models import Project, PullRequest, ReviewDraft
from franktheunicorn.review import StubReviewProvider, create_review_draft, set_provider


@pytest.fixture()
def sample_pr(db_session) -> PullRequest:
    p = Project(slug="test", repo="owner/repo")
    db_session.add(p)
    db_session.flush()
    pr = PullRequest(
        project_id=p.id,
        github_pr_number=42,
        title="Add feature X",
        author_login="alice",
        html_url="https://github.com/owner/repo/pull/42",
    )
    db_session.add(pr)
    db_session.flush()
    return pr


def test_stub_provider_returns_string(sample_pr):
    provider = StubReviewProvider()
    body = provider.generate_draft(sample_pr, review_context="", changed_files=[])
    assert isinstance(body, str)
    assert len(body) > 0


def test_stub_provider_rotates(sample_pr):
    provider = StubReviewProvider()
    comments = [provider.generate_draft(sample_pr, "", []) for _ in range(8)]
    # Should not all be the same (rotation).
    assert len(set(comments)) > 1


def test_stub_provider_includes_file_list(sample_pr):
    provider = StubReviewProvider()
    body = provider.generate_draft(
        sample_pr, "", changed_files=["src/core/engine.py", "tests/test_engine.py"]
    )
    assert "src/core/engine.py" in body


def test_create_review_draft_returns_orm_object(sample_pr):
    draft = create_review_draft(sample_pr)
    assert isinstance(draft, ReviewDraft)
    assert draft.pull_request_id == sample_pr.id
    assert draft.status == "pending"
    assert draft.source == "stub"
    assert len(draft.body) > 0


def test_set_provider_swaps_default(sample_pr):
    class CustomProvider:
        def generate_draft(self, pr, review_context, changed_files):
            return "custom draft"

    set_provider(CustomProvider())
    draft = create_review_draft(sample_pr)
    assert draft.body == "custom draft"

    # Restore default.
    set_provider(StubReviewProvider())
