"""Tests for the comment scraper."""

from __future__ import annotations

import httpx
import pytest

from franktheunicorn.curator.scraper import (
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


@pytest.fixture
def httpx_mock(httpx_mock):
    """Re-export pytest-httpx fixture."""
    return httpx_mock
