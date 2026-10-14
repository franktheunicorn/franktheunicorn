"""Tests for GitHub review posting and recall."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
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
        mock_client.list_pull_request_reviews.return_value = []

        poster = GitHubPoster(mock_client)
        result = poster.post_review(pr, [draft])

        assert result is None
        draft.refresh_from_db()
        assert draft.status == "accepted"

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


def _diff_showing(path: str, lines: range) -> str:
    """A unified diff whose only hunk adds ``lines`` of ``path``."""
    body = "\n".join(f"+line {n}" for n in lines)
    return f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +{lines.start},{len(lines)} @@\n{body}\n"


@pytest.mark.django_db
class TestMentionsAreQuotedBeforePosting:
    def test_handles_in_the_body_are_backticked_and_reported(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="src/main.py",
            line_number=10,
            comment_body="@alice wrote this; cc @apache/committers",
            status="accepted",
        )
        mock_client = MagicMock()
        mock_client.create_review.return_value = {"id": 1, "comment_ids_by_key": {}}

        result = GitHubPoster(mock_client).post_review(pr, [draft])

        review = mock_client.create_review.call_args.args[3]
        assert review.comments[0].body.startswith("`@alice` wrote this; cc `@apache/committers`")
        assert result is not None
        assert result["mentions_escaped"] == ["alice", "apache/committers"]

    def test_the_allowlist_keeps_a_handle_live(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="a.py",
            line_number=1,
            comment_body="cc @alice",
            status="accepted",
        )
        mock_client = MagicMock()
        mock_client.create_review.return_value = {"id": 1}

        result = GitHubPoster(mock_client, mention_allowlist=["@Alice"]).post_review(pr, [draft])

        review = mock_client.create_review.call_args.args[3]
        assert review.comments[0].body.startswith("cc @alice")
        assert result is not None
        assert result["mentions_escaped"] == []


@pytest.mark.django_db
class TestMovedHeadGuard:
    def _client(self, live_head: str, diff: str = "") -> MagicMock:
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": live_head}}
        client.get_pull_request_diff.return_value = diff
        client.create_review.return_value = {"id": 7, "comment_ids_by_key": {}}
        return client

    def test_a_moved_head_refuses_by_default(self) -> None:
        from franktheunicorn.backends.poster import StaleHeadError

        pr = PullRequestFactory(head_sha="aaaa1111aaaa1111")
        draft = ReviewDraftFactory(
            pull_request=pr, file_path="a.py", line_number=3, status="accepted"
        )
        client = self._client("bbbb2222bbbb2222")

        with pytest.raises(StaleHeadError) as excinfo:
            GitHubPoster(client).post_review(pr, [draft])

        assert excinfo.value.stored == "aaaa1111aaaa1111"
        assert excinfo.value.live == "bbbb2222bbbb2222"
        client.create_review.assert_not_called()
        draft.refresh_from_db()
        assert draft.status == "accepted"

    def test_the_same_head_posts_and_a_short_stored_sha_still_matches(self) -> None:
        pr = PullRequestFactory(head_sha="aaaa1111")
        draft = ReviewDraftFactory(
            pull_request=pr, file_path="a.py", line_number=3, status="accepted"
        )
        client = self._client("aaaa1111aaaa1111aaaa", _diff_showing("a.py", range(1, 6)))

        assert GitHubPoster(client).post_review(pr, [draft]) is not None
        client.create_review.assert_called_once()

    def test_post_anyway_keeps_anchored_lines_and_folds_the_rest(self) -> None:
        pr = PullRequestFactory(head_sha="aaaa1111")
        kept = ReviewDraftFactory(
            pull_request=pr, file_path="a.py", line_number=3, status="accepted", comment_body="k"
        )
        gone = ReviewDraftFactory(
            pull_request=pr, file_path="a.py", line_number=40, status="accepted", comment_body="g"
        )
        client = self._client("bbbb2222", _diff_showing("a.py", range(1, 6)))

        result = GitHubPoster(client).post_review(pr, [kept, gone], on_stale_head="post")

        review = client.create_review.call_args.args[3]
        by_key = {c.correlation_key: c for c in review.comments}
        assert by_key[str(kept.pk)].line == 3
        assert by_key[str(gone.pk)].line is None
        assert by_key[str(gone.pk)].body.startswith("_(on `a.py` line 40, which the current diff")
        assert result is not None
        assert result["folded_into_body"] == 1

    def test_body_only_folds_everything(self) -> None:
        pr = PullRequestFactory(head_sha="aaaa1111")
        draft = ReviewDraftFactory(
            pull_request=pr, file_path="a.py", line_number=3, line_end=5, status="accepted"
        )
        client = self._client("bbbb2222", _diff_showing("a.py", range(1, 6)))

        result = GitHubPoster(client).post_review(pr, [draft], on_stale_head="body-only")

        review = client.create_review.call_args.args[3]
        assert review.comments[0].line is None
        assert "line 3-5, posted body-only" in review.comments[0].body
        assert result is not None
        assert result["folded_into_body"] == 1

    def test_an_unreadable_head_or_diff_posts_as_drafted(self) -> None:
        pr = PullRequestFactory(head_sha="aaaa1111")
        draft = ReviewDraftFactory(
            pull_request=pr, file_path="a.py", line_number=99, status="accepted"
        )
        client = MagicMock()
        client.get_pull_request.side_effect = RuntimeError("boom")
        client.get_pull_request_diff.side_effect = RuntimeError("boom")
        client.create_review.return_value = {"id": 1}

        result = GitHubPoster(client).post_review(pr, [draft])

        review = client.create_review.call_args.args[3]
        assert review.comments[0].line == 99
        assert result is not None
        assert result["folded_into_body"] == 0

    def test_an_unknown_mode_is_rejected(self) -> None:
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(pull_request=pr, status="accepted")
        with pytest.raises(ValueError, match="on_stale_head"):
            GitHubPoster(MagicMock()).post_review(pr, [draft], on_stale_head="yolo")


@pytest.mark.django_db
class TestVerifyAfterAFailedPost:
    """A forge can accept a review and still fail the request, and a submitted
    review cannot be deleted. So a failure is checked, never blindly retried,
    and the check looks for this attempt, not for any review of frank's."""

    MARKER_BODY = "x\n<!-- franktheunicorn-managed -->"

    def _setup(self) -> tuple[Any, Any, MagicMock, list[Any]]:
        """A forge that times out on the post; ``sent`` captures what went out."""
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr, file_path="a.py", line_number=1, status="accepted"
        )
        client = MagicMock()
        client.get_pull_request_diff.return_value = ""
        sent: list[Any] = []

        def _create(owner: str, repo: str, number: int, review: Any) -> Any:
            sent.append(review)
            raise TimeoutError("read timed out")

        client.create_review.side_effect = _create
        return pr, draft, client, sent

    def test_a_review_that_landed_anyway_is_recorded_as_posted(self) -> None:
        pr, draft, client, sent = self._setup()
        now = datetime.now(tz=UTC).isoformat()
        client.list_pull_request_reviews.return_value = [
            {"id": 77, "body": "", "submitted_at": now}
        ]
        client.get_review_comments.side_effect = lambda *a: [
            {"id": 5, "body": c.body} for c in sent[0].comments
        ]

        result = GitHubPoster(client).post_review(pr, [draft])

        assert result is not None
        assert result["id"] == 77
        assert result["verified_after_error"] is True
        draft.refresh_from_db()
        assert draft.status == "posted"
        # The comment id came back with it, so the operator can still recall it.
        assert draft.forge_comment_id == 5

    def test_this_attempts_marker_in_the_review_body_is_enough(self) -> None:
        pr, draft, client, sent = self._setup()
        client.get_review_comments.return_value = []
        client.list_pull_request_reviews.side_effect = lambda *a: [
            {
                "id": 78,
                "body": "\n".join(c.body for c in sent[0].comments),
                "submitted_at": "2999-01-01T00:00:00Z",
            }
        ]
        assert GitHubPoster(client).post_review(pr, [draft]) is not None

    def test_an_earlier_review_of_ours_in_the_same_minute_does_not_count(self) -> None:
        """Found in review: the operator's previous Post moments earlier carries
        frank's marker too, and used to be read as this attempt landing."""
        pr, draft, client, _sent = self._setup()
        client.list_pull_request_reviews.return_value = [
            {"id": 1, "body": self.MARKER_BODY, "submitted_at": datetime.now(tz=UTC).isoformat()}
        ]
        client.get_review_comments.return_value = [{"id": 9, "body": self.MARKER_BODY}]

        assert GitHubPoster(client).post_review(pr, [draft]) is None
        draft.refresh_from_db()
        assert draft.status == "accepted"

    def test_someone_elses_review_does_not_count(self) -> None:
        pr, draft, client, _sent = self._setup()
        client.list_pull_request_reviews.return_value = [
            {"id": 2, "body": "LGTM", "submitted_at": datetime.now(tz=UTC).isoformat()}
        ]
        client.get_review_comments.return_value = [{"body": "nice"}]
        assert GitHubPoster(client).post_review(pr, [draft]) is None

    def test_each_attempt_carries_its_own_marker(self) -> None:
        pr, draft, client, sent = self._setup()
        client.list_pull_request_reviews.return_value = []
        poster = GitHubPoster(client)
        poster.post_review(pr, [draft])
        poster.post_review(pr, [draft])

        first, second = (review.comments[0].body for review in sent)
        assert "franktheunicorn-attempt:" in first
        assert (
            first.rsplit("franktheunicorn-attempt:", 1)[1]
            != second.rsplit("franktheunicorn-attempt:", 1)[1]
        )
        assert f":{draft.pk} -->" in first

    def test_an_unreadable_pr_is_unknown_not_absent(self) -> None:
        from franktheunicorn.backends.poster import PostUnverifiedError

        pr, draft, client, _sent = self._setup()
        client.list_pull_request_reviews.side_effect = RuntimeError("502")
        with pytest.raises(PostUnverifiedError):
            GitHubPoster(client).post_review(pr, [draft])
        draft.refresh_from_db()
        assert draft.status == "accepted"
