"""Tests for the GitHub API client (httpx wrapper)."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.backends.base import ReviewBody, ReviewComment
from franktheunicorn.backends.github import GitHubClient, infer_github_username


class TestGitHubClient:
    @pytest.fixture
    def mock_base_url(self) -> str:
        return "https://api.github.test"

    @pytest.fixture
    def client(self, mock_base_url: str) -> GitHubClient:
        c = GitHubClient(token="test-token", base_url=mock_base_url)
        yield c
        c.close()

    def test_get_pull_request(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json={"number": 42, "mergeable": True})
        result = client.get_pull_request("org", "repo", 42)
        assert result["number"] == 42
        assert result["mergeable"] is True

    def test_create_review(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json={"id": 99})
        result = client.create_review("org", "repo", 42, ReviewBody(event="COMMENT"))
        assert result["id"] == 99
        # No inline comments → no follow-up GET, mapping is empty.
        assert result["comment_ids_by_key"] == {}

    def test_create_review_translates_inline_comments(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        # POST review.
        httpx_mock.add_response(
            url="https://api.github.test/repos/org/repo/pulls/42/reviews",
            method="POST",
            json={"id": 1},
        )
        # Follow-up GET for comment IDs.
        httpx_mock.add_response(
            url="https://api.github.test/repos/org/repo/pulls/42/reviews/1/comments?per_page=100",
            method="GET",
            json=[{"id": 1001}, {"id": 1002}],
        )
        review = ReviewBody(
            event="COMMENT",
            body="overall",
            comments=[
                ReviewComment(path="a.py", body="single", correlation_key="c1", line=5),
                ReviewComment(
                    path="b.py", body="range", correlation_key="c2", line=10, line_end=15
                ),
            ],
        )
        result = client.create_review("org", "repo", 42, review)
        assert result["comment_ids_by_key"] == {"c1": 1001, "c2": 1002}

        post_req = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        import json as _json

        sent = _json.loads(post_req.content)
        assert sent["event"] == "COMMENT"
        assert sent["body"] == "overall"
        assert sent["comments"][0] == {"path": "a.py", "body": "single", "line": 5, "side": "RIGHT"}
        assert sent["comments"][1] == {
            "path": "b.py",
            "body": "range",
            "line": 15,
            "side": "RIGHT",
            "start_line": 10,
        }

    def test_get_review_comments(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json=[{"id": 1, "body": "nit"}])
        result = client.get_review_comments("org", "repo", 42, 99)
        assert len(result) == 1
        assert result[0]["body"] == "nit"

    def test_get_issue_comments(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json=[{"id": 10, "body": "thanks"}])
        result = client.get_issue_comments("org", "repo", 42)
        assert len(result) == 1

    def test_get_issue_comments_with_since(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        httpx_mock.add_response(json=[])
        result = client.get_issue_comments("org", "repo", 42, since="2026-01-01T00:00:00Z")
        assert result == []

    def test_delete_review_comment(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(status_code=204)
        # pr_number is unused on GitHub but required by the abstract signature.
        client.delete_review_comment("org", "repo", 42, 123)

    def test_get_authenticated_user(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json={"login": "holdenk"})
        result = client.get_authenticated_user()
        assert result["login"] == "holdenk"

    def test_get_pull_request_diff(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(text="diff --git a/file.py b/file.py\n")
        result = client.get_pull_request_diff("org", "repo", 42)
        assert "diff --git" in result


class TestInferGitHubUsername:
    def test_returns_login_on_success(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(json={"login": "holdenk"})
        result = infer_github_username("test-token", base_url="https://api.github.test")
        assert result == "holdenk"

    def test_returns_empty_on_no_token(self) -> None:
        assert infer_github_username("") == ""

    def test_returns_empty_on_failure(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(status_code=401)
        result = infer_github_username("bad-token", base_url="https://api.github.test")
        assert result == ""
