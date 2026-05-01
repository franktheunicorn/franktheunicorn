"""Tests for GitHub review posting (§3, §11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from franktheunicorn.backends.poster import (
    DEFAULT_ATTRIBUTION,
    MANAGED_MARKER,
    GitHubPoster,
    _format_comment_body,
    _format_suggestion_block,
)
from tests.factories import PullRequestFactory, ReviewDraftFactory


class TestFormatSuggestionBlock:
    def test_wraps_in_suggestion_markdown(self) -> None:
        result = _format_suggestion_block("new_code()")
        assert "```suggestion" in result
        assert "new_code()" in result
        assert result.count("```") == 2


@pytest.mark.django_db
class TestFormatCommentBody:
    def test_includes_attribution_and_marker(self) -> None:
        draft = ReviewDraftFactory(comment_body="Good point.", suggestion="")
        body = _format_comment_body(draft)
        assert DEFAULT_ATTRIBUTION in body
        assert MANAGED_MARKER in body
        assert "Good point." in body

    def test_includes_suggestion_block(self) -> None:
        draft = ReviewDraftFactory(comment_body="Consider this:", suggestion="better_code()")
        body = _format_comment_body(draft)
        assert "```suggestion" in body
        assert "better_code()" in body

    def test_uses_edited_body_when_present(self) -> None:
        draft = ReviewDraftFactory(comment_body="Original.", edited_body="Edited version.")
        body = _format_comment_body(draft)
        assert "Edited version." in body
        assert "Original." not in body


@pytest.mark.django_db
class TestGitHubPoster:
    def _make_poster(self) -> tuple[GitHubPoster, MagicMock]:
        client = MagicMock()
        client.create_review.return_value = {"id": 42, "comment_ids": [101, 102]}
        return GitHubPoster(client), client

    def test_post_review_creates_review(self) -> None:
        poster, client = self._make_poster()
        pr = PullRequestFactory()
        d1 = ReviewDraftFactory(
            pull_request=pr,
            file_path="a.py",
            line_number=10,
            comment_body="Fix this.",
            status="accepted",
        )
        d2 = ReviewDraftFactory(
            pull_request=pr,
            file_path="b.py",
            line_number=20,
            comment_body="And this.",
            status="accepted",
        )

        result = poster.post_review(pr, [d1, d2])

        assert result["id"] == 42
        client.create_review.assert_called_once()
        call_args = client.create_review.call_args
        body = call_args[0][3]
        assert len(body.comments) == 2
        assert body.comments[0].path == "a.py"
        assert body.comments[1].path == "b.py"

    def test_post_review_updates_draft_status(self) -> None:
        poster, _ = self._make_poster()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="a.py",
            line_number=5,
            comment_body="Fix.",
            status="accepted",
        )

        poster.post_review(pr, [draft])

        draft.refresh_from_db()
        assert draft.status == "posted"
        assert draft.posted_at is not None
        assert draft.github_comment_id == 101

    def test_post_review_returns_none_for_empty(self) -> None:
        poster, _ = self._make_poster()
        pr = PullRequestFactory()
        result = poster.post_review(pr, [])
        assert result is None

    def test_multi_line_comment(self) -> None:
        poster, client = self._make_poster()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="a.py",
            line_number=10,
            line_end=15,
            comment_body="Multi-line.",
            status="accepted",
        )

        poster.post_review(pr, [draft])

        body = client.create_review.call_args[0][3]
        comment = body.comments[0]
        assert comment.line == 10
        assert comment.line_end == 15

    def test_recall_within_window(self) -> None:
        poster, client = self._make_poster()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="a.py",
            comment_body="Recall me.",
            status="posted",
            github_comment_id=999,
            posted_at=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        result = poster.recall_comment(draft)
        assert result is True
        client.delete_review_comment.assert_called_once()
        draft.refresh_from_db()
        assert draft.status == "recalled"

    def test_recall_outside_window(self) -> None:
        poster, client = self._make_poster()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="a.py",
            comment_body="Too late.",
            status="posted",
            github_comment_id=999,
            posted_at=datetime.now(tz=UTC) - timedelta(hours=48),
        )

        result = poster.recall_comment(draft)
        assert result is False
        client.delete_review_comment.assert_not_called()

    def test_recall_no_comment_id(self) -> None:
        poster, _ = self._make_poster()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            comment_body="No id.",
            status="posted",
        )

        result = poster.recall_comment(draft)
        assert result is False
