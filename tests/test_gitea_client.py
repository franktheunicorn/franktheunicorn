"""Tests for the Gitea/Forgejo API client."""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.backends.base import ReviewBody, ReviewComment
from franktheunicorn.backends.gitea import GiteaClient, _normalize_base_url


class TestNormalizeBaseUrl:
    def test_appends_api_v1(self) -> None:
        assert _normalize_base_url("https://codeberg.org") == "https://codeberg.org/api/v1"

    def test_strips_trailing_slash(self) -> None:
        assert _normalize_base_url("https://codeberg.org/") == "https://codeberg.org/api/v1"

    def test_idempotent(self) -> None:
        assert _normalize_base_url("https://codeberg.org/api/v1") == "https://codeberg.org/api/v1"


class TestGiteaClient:
    @pytest.fixture
    def client(self) -> GiteaClient:
        c = GiteaClient(token="test-token", base_url="https://codeberg.test")
        yield c
        c.close()

    def test_requires_base_url(self) -> None:
        with pytest.raises(ValueError, match="requires base_url"):
            GiteaClient(token="t")

    def test_uses_token_auth_header(self, httpx_mock: HTTPXMock, client: GiteaClient) -> None:
        httpx_mock.add_response(json={"login": "alice"})
        client.get_authenticated_user()
        request = httpx_mock.get_request()
        assert request is not None
        assert request.headers["authorization"] == "token test-token"

    def test_list_pull_requests(self, httpx_mock: HTTPXMock, client: GiteaClient) -> None:
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls?state=open&limit=50",
            json=[
                {
                    "number": 7,
                    "title": "fix things",
                    "html_url": "https://codeberg.test/org/repo/pulls/7",
                    "user": {"login": "bob"},
                }
            ],
        )
        result = client.list_pull_requests("org", "repo")
        assert len(result) == 1
        # diff_url is synthesized for the poller's benefit.
        assert result[0]["diff_url"] == "https://codeberg.test/org/repo/pulls/7.diff"

    def test_get_pull_request(self, httpx_mock: HTTPXMock, client: GiteaClient) -> None:
        httpx_mock.add_response(json={"number": 7, "mergeable": True, "html_url": "h"})
        result = client.get_pull_request("org", "repo", 7)
        assert result["number"] == 7
        assert result["mergeable"] is True
        assert result["diff_url"] == "h.diff"

    def test_get_pull_request_diff(self, httpx_mock: HTTPXMock, client: GiteaClient) -> None:
        httpx_mock.add_response(text="diff --git a/x b/x\n")
        result = client.get_pull_request_diff("org", "repo", 7)
        assert "diff --git" in result

    def test_create_review_translates_inline_comment(
        self, httpx_mock: HTTPXMock, client: GiteaClient
    ) -> None:
        # First call: fetch the diff for position translation.
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "index 1..2 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -10,2 +10,3 @@\n"
            " ctx\n"
            "+added\n"
            " ctx2\n"
        )
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7.diff",
            text=diff,
        )
        # Second call: review-create.
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7/reviews",
            method="POST",
            json={"id": 99, "state": "COMMENTED"},
        )
        # Third call: post-review fetch of comment IDs.
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7/reviews/99/comments",
            method="GET",
            json=[{"id": 5001}],
        )

        review = ReviewBody(
            event="COMMENT",
            body="overall",
            comments=[ReviewComment(path="foo.py", body="nit", correlation_key="k1", line=11)],
        )
        result = client.create_review("org", "repo", 7, review)
        assert result["id"] == 99
        assert result["comment_ids_by_key"] == {"k1": 5001}

        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        sent = json.loads(post.content)
        assert sent["event"] == "COMMENT"
        assert sent["body"] == "overall"
        assert len(sent["comments"]) == 1
        assert sent["comments"][0] == {
            "path": "foo.py",
            "body": "nit",
            "new_position": 2,
        }

    def test_create_review_drops_unlocatable_comment(
        self, httpx_mock: HTTPXMock, client: GiteaClient
    ) -> None:
        diff = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -10,1 +10,1 @@\n ctx\n"
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7.diff", text=diff
        )
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7/reviews",
            method="POST",
            json={"id": 1},
        )

        review = ReviewBody(
            comments=[ReviewComment(path="foo.py", body="ghost", line=999)],
        )
        client.create_review("org", "repo", 7, review)

        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        sent = json.loads(post.content)
        assert sent["comments"] == []

    def test_create_review_dropped_middle_comment_keeps_alignment(
        self, httpx_mock: HTTPXMock, client: GiteaClient
    ) -> None:
        """If the middle comment can't be located, mapping stays deterministic.

        Regression for an index-shift bug where dropping a middle
        comment caused subsequent IDs to land on the wrong drafts and
        recall could delete the wrong forge comment.
        """
        # Diff that locates lines 11 and 13 but not line 999.
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -10,4 +10,5 @@\n"
            " ctx\n"
            "+a\n"
            " ctx2\n"
            "+c\n"
            " ctx3\n"
        )
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7.diff", text=diff
        )
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7/reviews",
            method="POST",
            json={"id": 50},
        )
        # Two posted comments → two IDs back, in posting order.
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7/reviews/50/comments",
            method="GET",
            json=[{"id": 7001}, {"id": 7003}],
        )

        review = ReviewBody(
            comments=[
                ReviewComment(path="foo.py", body="first", correlation_key="k1", line=11),
                ReviewComment(path="foo.py", body="ghost", correlation_key="k2", line=999),
                ReviewComment(path="foo.py", body="third", correlation_key="k3", line=13),
            ],
        )
        result = client.create_review("org", "repo", 7, review)
        assert result["comment_ids_by_key"] == {"k1": 7001, "k3": 7003}

    def test_create_review_no_inline_skips_diff_fetch(
        self, httpx_mock: HTTPXMock, client: GiteaClient
    ) -> None:
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7/reviews",
            method="POST",
            json={"id": 1},
        )
        review = ReviewBody(event="COMMENT", body="general only")
        client.create_review("org", "repo", 7, review)
        # Only one HTTP call (the POST). The diff fetch was skipped.
        assert len(httpx_mock.get_requests()) == 1

    def test_get_issue_comments_with_since(
        self, httpx_mock: HTTPXMock, client: GiteaClient
    ) -> None:
        httpx_mock.add_response(json=[])
        client.get_issue_comments("org", "repo", 7, since="2026-01-01T00:00:00Z")
        request = httpx_mock.get_request()
        assert request is not None
        assert b"since=2026-01-01" in request.url.query

    def test_create_review_multi_line_range_uses_end_line(
        self, httpx_mock: HTTPXMock, client: GiteaClient
    ) -> None:
        """Range comments resolve to the end-of-range line in the diff."""
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -10,3 +10,5 @@\n"
            " ctx\n"
            "+added1\n"
            "+added2\n"
            "+added3\n"
            " ctx2\n"
        )
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7.diff", text=diff
        )
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7/reviews",
            method="POST",
            json={"id": 1},
        )
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/7/reviews/1/comments",
            method="GET",
            json=[],
        )

        review = ReviewBody(
            comments=[ReviewComment(path="foo.py", body="multi", line=11, line_end=13)],
        )
        client.create_review("org", "repo", 7, review)

        post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        sent = json.loads(post.content)
        # @@ +1, +added1=2, +added2=3, +added3=4 → end-of-range line 13 → position 4.
        assert sent["comments"][0]["new_position"] == 4

    def test_delete_review_comment(self, httpx_mock: HTTPXMock, client: GiteaClient) -> None:
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/comments/123",
            method="DELETE",
            status_code=204,
        )
        client.delete_review_comment("org", "repo", 7, 123)
