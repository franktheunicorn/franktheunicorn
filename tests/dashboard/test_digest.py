"""Tests for the digest service."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.core.models import PullRequest
from franktheunicorn.digest.service import (
    DailyDigest,
    build_daily_digest,
    render_digest_text,
    send_digest,
)
from tests.factories import (
    CostRecordFactory,
    OperatorActionFactory,
    ReviewDraftFactory,
)


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

    def test_high_interest_section(self, db_pr: PullRequest) -> None:
        db_pr.interest_score = 0.85
        db_pr.save(update_fields=["interest_score"])
        digest = build_daily_digest(hours=9999)
        assert len(digest.high_interest) == 1
        assert digest.high_interest[0].interest_score == 0.85

    def test_your_prs_section(self, db_pr: PullRequest) -> None:
        db_pr.queue = "your-prs"
        db_pr.save(update_fields=["queue"])
        digest = build_daily_digest(hours=9999)
        assert len(digest.your_prs) == 1

    def test_ai_generated_section(self, db_pr: PullRequest) -> None:
        db_pr.queue = "ai-generated"
        db_pr.save(update_fields=["queue"])
        digest = build_daily_digest(hours=9999)
        assert len(digest.ai_generated) == 1

    def test_moderation_section(self, db_pr: PullRequest) -> None:
        db_pr.queue = "consider-closing"
        db_pr.save(update_fields=["queue"])
        digest = build_daily_digest(hours=9999)
        assert len(digest.moderation) == 1

    def test_weekly_stats(self, db_pr: PullRequest) -> None:
        OperatorActionFactory(action_type="accept_draft", pull_request=db_pr)
        OperatorActionFactory(action_type="reject_draft", pull_request=db_pr)
        CostRecordFactory(project=db_pr.project)
        digest = build_daily_digest(hours=9999)
        assert digest.stats.prs_reviewed == 2
        assert digest.stats.accuracy_pct == 50.0


@pytest.mark.django_db
class TestRenderDigestText:
    def test_render_empty(self) -> None:
        digest = DailyDigest()
        text = render_digest_text(digest)
        assert "franktheunicorn digest" in text
        assert "0 PRs need attention" in text

    def test_render_with_entries(self, db_pr: PullRequest) -> None:
        db_pr.interest_score = 0.9
        db_pr.save(update_fields=["interest_score"])
        digest = build_daily_digest(hours=9999)
        text = render_digest_text(digest)
        assert "HIGH-INTEREST" in text
        assert str(db_pr.number) in text

    def test_render_your_prs(self, db_pr: PullRequest) -> None:
        db_pr.queue = "your-prs"
        db_pr.save(update_fields=["queue"])
        digest = build_daily_digest(hours=9999)
        text = render_digest_text(digest)
        assert "YOUR PRs" in text

    def test_render_moderation(self, db_pr: PullRequest) -> None:
        db_pr.queue = "needs-triage"
        db_pr.save(update_fields=["queue"])
        digest = build_daily_digest(hours=9999)
        text = render_digest_text(digest)
        assert "MODERATION" in text

    def test_render_weekly_stats(self, db_pr: PullRequest) -> None:
        OperatorActionFactory(action_type="accept_draft", pull_request=db_pr)
        digest = build_daily_digest(hours=9999)
        text = render_digest_text(digest)
        assert "WEEKLY STATS" in text


@pytest.mark.django_db
class TestRenderDigestHtml:
    def test_render_empty(self) -> None:
        from franktheunicorn.digest.service import render_digest_html

        digest = DailyDigest()
        html = render_digest_html(digest)
        assert "franktheunicorn digest" in html

    def test_render_with_entries(self, db_pr: PullRequest) -> None:
        from franktheunicorn.digest.service import render_digest_html

        db_pr.interest_score = 0.85
        db_pr.save(update_fields=["interest_score"])
        digest = build_daily_digest(hours=9999)
        html = render_digest_html(digest)
        assert "HIGH-INTEREST" in html

    def test_render_ai_generated(self, db_pr: PullRequest) -> None:
        from franktheunicorn.digest.service import render_digest_html

        db_pr.queue = "ai-generated"
        db_pr.save(update_fields=["queue"])
        digest = build_daily_digest(hours=9999)
        html = render_digest_html(digest)
        assert "AI-GENERATED" in html


@pytest.mark.django_db
class TestSendDigest:
    def test_no_email_configured(self) -> None:
        with patch("franktheunicorn.digest.service.getattr", side_effect=lambda o, k, d="": d):
            result = send_digest(DailyDigest())
        assert result is False

    def test_sends_email(self, db_pr: PullRequest) -> None:
        digest = build_daily_digest(hours=9999)
        with patch("django.core.mail.send_mail") as mock_send:
            from django.conf import settings

            settings.FRANK_DIGEST_EMAIL = "test@example.com"
            try:
                result = send_digest(digest)
            finally:
                settings.FRANK_DIGEST_EMAIL = ""
        assert result is True
        mock_send.assert_called_once()
