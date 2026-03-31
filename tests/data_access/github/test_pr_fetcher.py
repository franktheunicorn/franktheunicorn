"""Tests for PRFetcher (API + scrape paths)."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError, RateLimitError
from franktheunicorn.data_access.github.pr_fetcher import (
    MergeStatusParseError,
    PRFetcher,
    _scrape_mergeable,
)
from franktheunicorn.data_access.github.types import PRSummary


class TestPRFetcherAPI:
    def test_fetches_pr_summary(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        result = pr_fetcher.fetch_via_api("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.number == 42
        assert result.title == "Fix flaky test in scheduler module"
        assert result.author == "alice-dev"
        assert result.state == "open"
        assert result.fetched_via == FetchMethod.API
        assert result.labels == ("bug", "tests")
        assert result.requested_reviewers == ("holdenk",)
        assert result.is_draft is False
        assert result.additions == 15
        assert result.deletions == 3

    def test_parses_files(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        result = pr_fetcher.fetch_via_api("apache", "spark", 42)

        assert len(result.files) == 2
        assert result.files[0].filename == "core/src/test/scala/SchedulerSuite.scala"
        assert result.files[0].additions == 12

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/999", status_code=404
        )
        with pytest.raises(NotFoundError):
            pr_fetcher.fetch_via_api("apache", "spark", 999)

    def test_403_raises_rate_limit(self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", status_code=403
        )
        with pytest.raises(RateLimitError):
            pr_fetcher.fetch_via_api("apache", "spark", 42)

    def test_handles_null_body(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        pr_api_json["body"] = None
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        assert pr_fetcher.fetch_via_api("apache", "spark", 42).body == ""


class TestPRFetcherScrape:
    def test_parses_all_fields(
        self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher, pr_scrape_html: str
    ) -> None:
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
        result = pr_fetcher.fetch_via_scrape("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.number == 42
        assert result.title == "Fix flaky test in scheduler module"
        assert result.author == "alice-dev"
        assert result.state == "open"
        assert result.fetched_via == FetchMethod.SCRAPE
        assert "bug" in result.labels
        assert "tests" in result.labels
        assert "race condition" in result.body

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher) -> None:
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/999", status_code=404)
        with pytest.raises(NotFoundError):
            pr_fetcher.fetch_via_scrape("apache", "spark", 999)


class TestPRFetcherAPIMergeable:
    def test_api_returns_mergeable_true(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        pr_api_json["mergeable"] = True
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        result = pr_fetcher.fetch_via_api("apache", "spark", 42)
        assert result.mergeable is True

    def test_api_returns_mergeable_false(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        pr_api_json["mergeable"] = False
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        result = pr_fetcher.fetch_via_api("apache", "spark", 42)
        assert result.mergeable is False

    def test_api_returns_mergeable_null(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        pr_api_json["mergeable"] = None  # GitHub returns null when not yet computed
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        result = pr_fetcher.fetch_via_api("apache", "spark", 42)
        assert result.mergeable is None

    def test_api_missing_mergeable_key(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        pr_api_json.pop("mergeable", None)
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        result = pr_fetcher.fetch_via_api("apache", "spark", 42)
        assert result.mergeable is None


class TestScrapeMergeable:
    """Tests for _scrape_mergeable HTML parsing."""

    def test_no_merge_box_returns_none(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body><p>No merge box</p></body></html>", "html.parser")
        assert _scrape_mergeable(soup) is None

    def test_has_conflicts(self) -> None:
        from bs4 import BeautifulSoup

        html = '<div class="merge-message">This branch has conflicts that must be resolved</div>'
        soup = BeautifulSoup(html, "html.parser")
        assert _scrape_mergeable(soup) is False

    def test_resolve_conflicts(self) -> None:
        from bs4 import BeautifulSoup

        html = '<div class="merge-message">You must resolve conflicts before merging</div>'
        soup = BeautifulSoup(html, "html.parser")
        assert _scrape_mergeable(soup) is False

    def test_no_conflicts(self) -> None:
        from bs4 import BeautifulSoup

        html = '<div class="merge-message">This branch has no conflicts with the base branch</div>'
        soup = BeautifulSoup(html, "html.parser")
        assert _scrape_mergeable(soup) is True

    def test_merge_button(self) -> None:
        from bs4 import BeautifulSoup

        html = '<div class="merge-message"><button>Merge pull request</button></div>'
        soup = BeautifulSoup(html, "html.parser")
        assert _scrape_mergeable(soup) is True

    def test_able_to_merge(self) -> None:
        from bs4 import BeautifulSoup

        html = '<div class="merge-message">Merging can be performed automatically</div>'
        soup = BeautifulSoup(html, "html.parser")
        assert _scrape_mergeable(soup) is True

    def test_unknown_merge_text_raises(self) -> None:
        from bs4 import BeautifulSoup

        html = '<div class="merge-message">Some totally new GitHub UI text here</div>'
        soup = BeautifulSoup(html, "html.parser")
        with pytest.raises(MergeStatusParseError):
            _scrape_mergeable(soup)

    def test_empty_merge_box_returns_none(self) -> None:
        from bs4 import BeautifulSoup

        html = '<div class="merge-message"></div>'
        soup = BeautifulSoup(html, "html.parser")
        assert _scrape_mergeable(soup) is None


class TestPRFetcherScrapeMergeable:
    def test_scrape_returns_mergeable(
        self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher, pr_scrape_html: str
    ) -> None:
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
        result = pr_fetcher.fetch_via_scrape("apache", "spark", 42)
        assert result.mergeable is True

    def test_scrape_conflict_html(self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher) -> None:
        html = """
        <html><body>
        <div class="gh-header-title"><h1><span class="js-issue-title">Test PR</span></h1></div>
        <div class="gh-header-meta"><a class="author">bob</a>
        <span class="State State--open">Open</span></div>
        <div class="comment-body">Body text</div>
        <div class="merge-message">This branch has conflicts that must be resolved</div>
        </body></html>
        """
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/99", text=html)
        result = pr_fetcher.fetch_via_scrape("apache", "spark", 99)
        assert result.mergeable is False


class TestPRFetcherFallback:
    def test_falls_back_to_scrape_on_403(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", status_code=403
        )
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
        result = pr_fetcher.fetch("apache", "spark", 42)

        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.number == 42
