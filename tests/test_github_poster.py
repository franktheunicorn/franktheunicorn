"""Tests for GitHub review posting and recall."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from franktheunicorn.github.poster import GitHubPoster
from tests.factories import PullRequestFactory, ReviewDraftFactory


@pytest.mark.django_db
class TestGitHubPoster:
    def test_post_review_success(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="src/main.py",
            line_number=10,
            comment_body="Consider a test.",
            status="accepted",
        )

        mock_client = MagicMock()
        mock_client.create_review.return_value = {"id": 42}
        mock_client.get_review_comments.return_value = [{"id": 101}]

        poster = GitHubPoster(mock_client)
        result = poster.post_review(pr, [draft])

        assert result is not None
        assert result["id"] == 42
        draft.refresh_from_db()
        assert draft.status == "posted"
        assert draft.github_comment_id == 101
        assert draft.posted_at is not None

    def test_post_review_api_failure(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            status="accepted",
        )

        mock_client = MagicMock()
        mock_client.create_review.side_effect = Exception("API error")

        poster = GitHubPoster(mock_client)
        result = poster.post_review(pr, [draft])

        assert result is None

    def test_post_review_empty_drafts(self) -> None:
        pr = PullRequestFactory()
        mock_client = MagicMock()
        poster = GitHubPoster(mock_client)
        result = poster.post_review(pr, [])
        assert result is None

    def test_post_review_comment_id_fetch_fails(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(pull_request=pr, status="accepted")

        mock_client = MagicMock()
        mock_client.create_review.return_value = {"id": 42}
        mock_client.get_review_comments.side_effect = Exception("timeout")

        poster = GitHubPoster(mock_client)
        result = poster.post_review(pr, [draft])

        assert result is not None
        draft.refresh_from_db()
        assert draft.status == "posted"
        # comment_id not set because fetch failed
        assert draft.github_comment_id is None


@pytest.mark.django_db
class TestRecallComment:
    def test_recall_success(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            status="posted",
            github_comment_id=123,
            posted_at=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        mock_client = MagicMock()
        poster = GitHubPoster(mock_client)
        result = poster.recall_comment(draft)

        assert result is True
        draft.refresh_from_db()
        assert draft.status == "recalled"
        mock_client.delete_review_comment.assert_called_once()

    def test_recall_no_comment_id(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            status="posted",
            github_comment_id=None,
        )
        mock_client = MagicMock()
        poster = GitHubPoster(mock_client)
        assert poster.recall_comment(draft) is False

    def test_recall_no_posted_at(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            status="posted",
            github_comment_id=123,
            posted_at=None,
        )
        mock_client = MagicMock()
        poster = GitHubPoster(mock_client)
        assert poster.recall_comment(draft) is False

    def test_recall_outside_window(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            status="posted",
            github_comment_id=123,
            posted_at=datetime.now(tz=UTC) - timedelta(hours=25),
        )
        mock_client = MagicMock()
        poster = GitHubPoster(mock_client)
        assert poster.recall_comment(draft) is False
        mock_client.delete_review_comment.assert_not_called()

    def test_recall_api_failure(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            status="posted",
            github_comment_id=123,
            posted_at=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        mock_client = MagicMock()
        mock_client.delete_review_comment.side_effect = Exception("API error")
        poster = GitHubPoster(mock_client)
        assert poster.recall_comment(draft) is False
