"""Tests for the GitHub API client (httpx wrapper)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.backends.base import ReviewBody, ReviewComment
from franktheunicorn.backends.github import (
    GitHubClient,
    _list_pull_requests_via_scrape,
    infer_github_username,
)

_FIXTURES = Path(__file__).parent / "data_access" / "github" / "fixtures"


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


class TestListPullRequestsAuthFallback:
    """list_pull_requests falls back to scrape on 401/403 and logs suggestions."""

    @pytest.fixture
    def client(self) -> GitHubClient:
        c = GitHubClient(token="bad-token", base_url="https://api.github.test")
        yield c
        c.close()

    def test_401_logs_suggestions(
        self, httpx_mock: HTTPXMock, client: GitHubClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        html = (_FIXTURES / "pulls_listing_scrape.html").read_text()
        httpx_mock.add_response(
            url="https://api.github.test/repos/apache/spark/pulls?state=open&per_page=50",
            status_code=401,
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pulls?q=is%3Apr&state=open",
            text=html,
        )
        import logging

        with caplog.at_level(logging.ERROR, logger="franktheunicorn.backends.github"):
            client.list_pull_requests("apache", "spark")

        combined = " ".join(caplog.messages)
        assert "401" in combined
        assert "GITHUB_TOKEN" in combined
        assert "repo" in combined.lower()

    def test_401_returns_scraped_prs(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        html = (_FIXTURES / "pulls_listing_scrape.html").read_text()
        httpx_mock.add_response(
            url="https://api.github.test/repos/apache/spark/pulls?state=open&per_page=50",
            status_code=401,
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pulls?q=is%3Apr&state=open",
            text=html,
        )
        results = client.list_pull_requests("apache", "spark")
        assert len(results) == 2
        numbers = {r["number"] for r in results}
        assert numbers == {42, 99}
        for r in results:
            assert "title" in r
            assert "user" in r
            assert "html_url" in r

    def test_401_scrape_also_fails_returns_empty(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.test/repos/apache/spark/pulls?state=open&per_page=50",
            status_code=401,
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pulls?q=is%3Apr&state=open",
            status_code=503,
        )
        results = client.list_pull_requests("apache", "spark")
        assert results == []

    def test_403_logs_forbidden_and_falls_back(
        self, httpx_mock: HTTPXMock, client: GitHubClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        html = (_FIXTURES / "pulls_listing_scrape.html").read_text()
        httpx_mock.add_response(
            url="https://api.github.test/repos/apache/spark/pulls?state=open&per_page=50",
            status_code=403,
            headers={"X-OAuth-Scopes": "read:user"},
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pulls?q=is%3Apr&state=open",
            text=html,
        )
        import logging

        with caplog.at_level(logging.ERROR, logger="franktheunicorn.backends.github"):
            results = client.list_pull_requests("apache", "spark")

        combined = " ".join(caplog.messages)
        assert "403" in combined
        assert "public_repo" in combined or "repo" in combined
        # Should include the granted scopes in the hint
        assert "read:user" in combined
        assert len(results) == 2

    def test_403_no_scope_header_still_logs(
        self, httpx_mock: HTTPXMock, client: GitHubClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        html = (_FIXTURES / "pulls_listing_scrape.html").read_text()
        httpx_mock.add_response(
            url="https://api.github.test/repos/apache/spark/pulls?state=open&per_page=50",
            status_code=403,
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pulls?q=is%3Apr&state=open",
            text=html,
        )
        import logging

        with caplog.at_level(logging.ERROR, logger="franktheunicorn.backends.github"):
            results = client.list_pull_requests("apache", "spark")

        combined = " ".join(caplog.messages)
        assert "403" in combined
        # Fine-grained PAT note should appear
        assert "Pull requests" in combined
        assert len(results) == 2


class TestListPullRequestsViaScrape:
    """Unit tests for the standalone scrape helper."""

    def test_parses_pr_list_from_fixture(self, httpx_mock: HTTPXMock) -> None:
        html = (_FIXTURES / "pulls_listing_scrape.html").read_text()
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pulls?q=is%3Apr&state=open",
            text=html,
        )
        results = _list_pull_requests_via_scrape("apache", "spark")
        assert len(results) == 2
        pr42 = next(r for r in results if r["number"] == 42)
        assert pr42["title"] == "Fix flaky test in scheduler module"
        assert pr42["user"]["login"] == "alice-dev"
        assert pr42["state"] == "open"
        assert pr42["html_url"] == "https://github.com/apache/spark/pull/42"
        assert pr42["diff_url"] == "https://github.com/apache/spark/pull/42.diff"

    def test_empty_html_returns_empty_list(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pulls?q=is%3Apr&state=open",
            text="<html><body><p>No results</p></body></html>",
        )
        results = _list_pull_requests_via_scrape("apache", "spark")
        assert results == []

    def test_http_error_returns_empty_list(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pulls?q=is%3Apr&state=open",
            status_code=503,
        )
        results = _list_pull_requests_via_scrape("apache", "spark")
        assert results == []


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
