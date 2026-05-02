"""Tests for GitHub review posting and recall."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from franktheunicorn.backends.poster import GitHubPoster
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
        mock_client.create_review.return_value = {
            "id": 42,
            "comment_ids_by_key": {str(draft.pk): 101},
        }

        poster = GitHubPoster(mock_client)
        result = poster.post_review(pr, [draft])

        assert result is not None
        assert result["id"] == 42
        draft.refresh_from_db()
        assert draft.status == "posted"
        assert draft.forge_comment_id == 101
        assert draft.posted_at is not None
        # Poster no longer fetches comment IDs separately.
        mock_client.get_review_comments.assert_not_called()

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

    def test_post_review_no_comment_ids_in_response(self) -> None:
        """If the client returns no comment_ids (e.g. inline-fetch failed inside
        create_review), drafts are still marked posted but without IDs."""
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(pull_request=pr, status="accepted")

        mock_client = MagicMock()
        mock_client.create_review.return_value = {"id": 42, "comment_ids_by_key": {}}

        poster = GitHubPoster(mock_client)
        result = poster.post_review(pr, [draft])

        assert result is not None
        draft.refresh_from_db()
        assert draft.status == "posted"
        assert draft.forge_comment_id is None

    def test_post_review_matches_ids_by_correlation_key(self) -> None:
        """Returned IDs are matched by correlation key, not order/position."""
        pr = PullRequestFactory()
        d1 = ReviewDraftFactory(
            pull_request=pr, file_path="a.py", line_number=10, status="accepted"
        )
        d2 = ReviewDraftFactory(
            pull_request=pr, file_path="a.py", line_number=10, status="accepted"
        )
        d3 = ReviewDraftFactory(
            pull_request=pr, file_path="c.py", line_number=30, status="accepted"
        )

        mock_client = MagicMock()
        mock_client.create_review.return_value = {
            "id": 99,
            # Reordered result and dropped d2.
            "comment_ids_by_key": {str(d3.pk): 1003, str(d1.pk): 1001},
        }

        poster = GitHubPoster(mock_client)
        poster.post_review(pr, [d1, d2, d3])

        d1.refresh_from_db()
        d2.refresh_from_db()
        d3.refresh_from_db()
        assert d1.forge_comment_id == 1001
        # d2 was dropped — must not inherit any sibling ID.
        assert d2.forge_comment_id is None
        assert d2.status == "posted"
        assert d3.forge_comment_id == 1003


@pytest.mark.django_db
class TestRecallComment:
    def test_recall_success(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            status="posted",
            forge_comment_id=123,
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
            forge_comment_id=None,
        )
        mock_client = MagicMock()
        poster = GitHubPoster(mock_client)
        assert poster.recall_comment(draft) is False

    def test_recall_no_posted_at(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            status="posted",
            forge_comment_id=123,
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
            forge_comment_id=123,
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
            forge_comment_id=123,
            posted_at=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        mock_client = MagicMock()
        mock_client.delete_review_comment.side_effect = Exception("API error")
        poster = GitHubPoster(mock_client)
        assert poster.recall_comment(draft) is False
