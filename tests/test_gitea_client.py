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

        review = ReviewBody(
            event="COMMENT",
            body="overall",
            comments=[ReviewComment(path="foo.py", body="nit", line=11)],
        )
        result = client.create_review("org", "repo", 7, review)
        assert result["id"] == 99

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

    def test_delete_review_comment(self, httpx_mock: HTTPXMock, client: GiteaClient) -> None:
        httpx_mock.add_response(
            url="https://codeberg.test/api/v1/repos/org/repo/pulls/comments/123",
            method="DELETE",
            status_code=204,
        )
        client.delete_review_comment("org", "repo", 123)
