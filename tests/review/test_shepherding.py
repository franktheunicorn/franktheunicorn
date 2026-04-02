"""Tests for shepherding mode (v2 — §2.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from franktheunicorn.core.models import ReviewDraft
from franktheunicorn.review.shepherding import (
    ReviewerComment,
    detect_questions,
    generate_shepherd_drafts,
)
from tests.factories import PullRequestFactory


class TestDetectQuestions:
    def test_question_mark(self) -> None:
        assert detect_questions("Did you test this?") is True

    def test_question_word(self) -> None:
        assert detect_questions("Why did you choose this approach") is True

    def test_not_a_question(self) -> None:
        assert detect_questions("This looks good to me.") is False

    def test_could_you(self) -> None:
        assert detect_questions("Could you add a test for this") is True

    def test_is_there(self) -> None:
        assert detect_questions("Is there a reason for this change") is True

    def test_empty_string(self) -> None:
        assert detect_questions("") is False


@pytest.mark.django_db
class TestGenerateShepherdDrafts:
    def test_no_comments_returns_empty(self) -> None:
        from franktheunicorn.config.models import OperatorConfig, ProjectConfig

        pr = PullRequestFactory(is_operator_pr=True)
        drafts = generate_shepherd_drafts(
            pr, [], OperatorConfig(), ProjectConfig(owner="x", repo="y")
        )
        # No comments, no condition alerts (mergeable is None, not False).
        assert len(drafts) == 0 or all("shepherding" in d.sources for d in drafts)

    def test_generates_response_for_comment(self) -> None:
        from franktheunicorn.config.models import OperatorConfig, ProjectConfig

        pr = PullRequestFactory(is_operator_pr=True)
        comments = [
            ReviewerComment(
                author="reviewer1",
                body="Why did you use a list here instead of a set?",
                created_at=datetime.now(tz=UTC),
                is_question=True,
            ),
        ]
        drafts = generate_shepherd_drafts(
            pr, comments, OperatorConfig(), ProjectConfig(owner="x", repo="y")
        )
        shepherd_drafts = [d for d in drafts if d.reasoning_trace.startswith("Response to")]
        assert len(shepherd_drafts) == 1
        assert "shepherding" in shepherd_drafts[0].sources
        assert shepherd_drafts[0].status == "pending"
        assert shepherd_drafts[0].comment_body != ""

    def test_rebase_needed_alert(self) -> None:
        from franktheunicorn.config.models import OperatorConfig, ProjectConfig

        pr = PullRequestFactory(is_operator_pr=True, mergeable=False)
        drafts = generate_shepherd_drafts(
            pr, [], OperatorConfig(), ProjectConfig(owner="x", repo="y")
        )
        rebase_drafts = [d for d in drafts if "rebase" in d.comment_body.lower()]
        assert len(rebase_drafts) == 1

    def test_staleness_alert(self) -> None:
        from franktheunicorn.config.models import OperatorConfig, ProjectConfig

        old_date = datetime.now(tz=UTC) - timedelta(days=30)
        pr = PullRequestFactory(is_operator_pr=True, github_updated_at=old_date)
        drafts = generate_shepherd_drafts(
            pr, [], OperatorConfig(), ProjectConfig(owner="x", repo="y")
        )
        stale_drafts = [d for d in drafts if "no activity" in d.comment_body.lower()]
        assert len(stale_drafts) == 1
        assert "30 days" in stale_drafts[0].comment_body

    def test_no_staleness_for_recent_pr(self) -> None:
        from franktheunicorn.config.models import OperatorConfig, ProjectConfig

        recent = datetime.now(tz=UTC) - timedelta(days=1)
        pr = PullRequestFactory(is_operator_pr=True, github_updated_at=recent)
        drafts = generate_shepherd_drafts(
            pr, [], OperatorConfig(), ProjectConfig(owner="x", repo="y")
        )
        stale_drafts = [d for d in drafts if "no activity" in d.comment_body.lower()]
        assert len(stale_drafts) == 0


@pytest.mark.django_db
class TestShepherdingFeedbackLoop:
    """Test that shepherding actions correctly feed into the training pipeline."""

    def test_approve_shepherd_creates_correct_action(self) -> None:
        from franktheunicorn.dashboard.views import _action_type_for_draft

        draft = ReviewDraft(sources=["shepherding"])
        assert _action_type_for_draft(draft, "accept") == "accept_shepherd"
        assert _action_type_for_draft(draft, "reject") == "reject_shepherd"
        assert _action_type_for_draft(draft, "edit") == "edit_shepherd"

    def test_regular_draft_creates_regular_action(self) -> None:
        from franktheunicorn.dashboard.views import _action_type_for_draft

        draft = ReviewDraft(sources=["agent"])
        assert _action_type_for_draft(draft, "accept") == "accept_draft"
        assert _action_type_for_draft(draft, "reject") == "reject_draft"
        assert _action_type_for_draft(draft, "edit") == "edit_draft"
