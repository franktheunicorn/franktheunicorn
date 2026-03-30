"""Tests for the digest service."""

from __future__ import annotations

import pytest
from tests.factories import ReviewDraftFactory

from franktheunicorn.core.models import PullRequest
from franktheunicorn.digest.service import DailyDigest, build_daily_digest


@pytest.mark.django_db
class TestDigestService:
    def test_empty_digest(self) -> None:
        digest = build_daily_digest()
        assert isinstance(digest, DailyDigest)
        assert digest.total_prs_reviewed == 0
        assert digest.entries == []

    def test_digest_with_prs(self, db_pr: PullRequest) -> None:
        digest = build_daily_digest(hours=9999)  # wide window
        assert digest.total_prs_reviewed == 1
        assert digest.entries[0].pr_number == 42

    def test_digest_counts_pending_drafts(self, db_pr: PullRequest) -> None:
        ReviewDraftFactory(
            pull_request=db_pr,
            comment_body="Test draft",
            status="pending",
        )
        digest = build_daily_digest(hours=9999)
        assert digest.total_drafts_pending == 1
