"""Tests for the comment scraper."""

from __future__ import annotations

import httpx
import pytest

from franktheunicorn.curator.scraper import (
    RawComment,
    _extract_pr_number,
    scrape_review_comments,
)


def _make_api_comment(
    login: str = "alice",
    body: str = "Looks good",
    path: str = "src/main.py",
    pr_url: str = "https://api.github.com/repos/org/repo/pulls/42",
    diff_hunk: str = "@@ -1,3 +1,5 @@\n+new line",
    created_at: str = "2026-03-20T10:00:00Z",
    html_url: str = "https://github.com/org/repo/pull/42#discussion_r1",
) -> dict:
    return {
        "user": {"login": login},
        "body": body,
        "path": path,
        "pull_request_url": pr_url,
        "diff_hunk": diff_hunk,
        "created_at": created_at,
        "html_url": html_url,
    }


class TestExtractPrNumber:
    def test_valid_url(self) -> None:
        url = "https://api.github.com/repos/apache/spark/pulls/42"
        assert _extract_pr_number(url) == 42

    def test_empty_url(self) -> None:
        assert _extract_pr_number("") == 0

    def test_invalid_url(self) -> None:
        assert _extract_pr_number("https://example.com/no-number/here") == 0


class TestScrapeReviewComments:
    def test_scrapes_comments(self, httpx_mock) -> None:
        api_comments = [
            _make_api_comment(login="alice", body="Fix the bug"),
            _make_api_comment(login="bob", body="LGTM"),
        ]
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=2&page=1"
            ),
            json=api_comments,
        )

        result = scrape_review_comments("org", "repo", "fake-token", limit=2)

        assert len(result) == 2
        assert result[0].author == "alice"
        assert result[0].body == "Fix the bug"
        assert result[0].file_path == "src/main.py"
        assert result[0].pr_number == 42
        assert result[1].author == "bob"

    def test_respects_limit(self, httpx_mock) -> None:
        api_comments = [_make_api_comment() for _ in range(5)]
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=3&page=1"
            ),
            json=api_comments,
        )

        result = scrape_review_comments("org", "repo", "fake-token", limit=3)

        assert len(result) == 3

    def test_handles_empty_response(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=100&page=1"
            ),
            json=[],
        )

        result = scrape_review_comments("org", "repo", "fake-token")

        assert result == []

    def test_pagination(self, httpx_mock) -> None:
        page1 = [_make_api_comment(login=f"user-{i}") for i in range(3)]
        page2 = [_make_api_comment(login=f"user-{i + 3}") for i in range(2)]
        page3: list[dict] = []

        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=100&page=1"
            ),
            json=page1,
        )
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=100&page=2"
            ),
            json=page2,
        )
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=100&page=3"
            ),
            json=page3,
        )

        result = scrape_review_comments("org", "repo", "fake-token", limit=100)

        # Should get page1 (3) + page2 (2) = 5, page3 empty stops pagination
        assert len(result) == 5

    def test_missing_fields_handled(self, httpx_mock) -> None:
        sparse_comment = {
            "user": {},
            "body": None,
            "path": None,
            "pull_request_url": "",
            "diff_hunk": None,
            "created_at": "",
            "html_url": "",
        }
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=1&page=1"
            ),
            json=[sparse_comment],
        )

        result = scrape_review_comments("org", "repo", "fake-token", limit=1)

        assert len(result) == 1
        assert result[0].author == ""
        assert result[0].body == ""
        assert result[0].pr_number == 0


class TestExtractPrNumberEdgeCases:
    def test_url_with_trailing_slash(self) -> None:
        # rstrip("/") removes trailing slash, so the number is still extracted
        url = "https://api.github.com/repos/org/repo/pulls/99/"
        assert _extract_pr_number(url) == 99

    def test_url_with_non_numeric_end(self) -> None:
        assert _extract_pr_number("https://example.com/pulls/abc") == 0

    def test_single_segment_url(self) -> None:
        assert _extract_pr_number("42") == 42

    def test_just_a_number(self) -> None:
        assert _extract_pr_number("123") == 123


class TestRawComment:
    def test_dataclass_fields(self) -> None:
        comment = RawComment(
            author="bob",
            body="LGTM",
            diff_context="@@ context",
            file_path="README.md",
            pr_number=7,
            pr_title="Update docs",
            created_at="2026-01-01T00:00:00Z",
            url="https://github.com/org/repo/pull/7#r1",
        )
        assert comment.author == "bob"
        assert comment.body == "LGTM"
        assert comment.diff_context == "@@ context"
        assert comment.file_path == "README.md"
        assert comment.pr_number == 7
        assert comment.pr_title == "Update docs"
        assert comment.created_at == "2026-01-01T00:00:00Z"
        assert comment.url == "https://github.com/org/repo/pull/7#r1"


class TestScrapeReviewCommentsErrorHandling:
    def test_http_error_raises(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=100&page=1"
            ),
            status_code=403,
        )

        with pytest.raises(httpx.HTTPStatusError):
            scrape_review_comments("org", "repo", "fake-token")

    def test_pagination_stops_at_limit_mid_page(self, httpx_mock) -> None:
        """When limit is reached mid-page, stop collecting."""
        page1 = [_make_api_comment(login=f"user-{i}") for i in range(5)]
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=2&page=1"
            ),
            json=page1,
        )

        result = scrape_review_comments("org", "repo", "fake-token", limit=2)

        assert len(result) == 2

    def test_missing_user_key_entirely(self, httpx_mock) -> None:
        """Comment with no user key at all."""
        sparse_comment = {
            "body": "Hello",
            "path": "test.py",
            "pull_request_url": "https://api.github.com/repos/org/repo/pulls/1",
            "diff_hunk": "@@ hunk",
            "created_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/org/repo/pull/1#r1",
        }
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=1&page=1"
            ),
            json=[sparse_comment],
        )

        result = scrape_review_comments("org", "repo", "fake-token", limit=1)

        assert len(result) == 1
        assert result[0].author == ""
        assert result[0].body == "Hello"

    def test_headers_include_auth(self, httpx_mock) -> None:
        """Verify the authorization header is set."""
        httpx_mock.add_response(
            url=httpx.URL(
                "https://api.github.com/repos/org/repo/pulls/comments"
                "?sort=created&direction=desc&per_page=1&page=1"
            ),
            json=[_make_api_comment()],
        )

        scrape_review_comments("org", "repo", "my-secret-token", limit=1)

        request = httpx_mock.get_request()
        assert request is not None
        assert request.headers["authorization"] == "Bearer my-secret-token"
        assert request.headers["x-github-api-version"] == "2022-11-28"


@pytest.fixture
def httpx_mock(httpx_mock):
    """Re-export pytest-httpx fixture."""
    return httpx_mock
