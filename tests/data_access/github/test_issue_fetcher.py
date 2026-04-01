"""Tests for GitHub issue fetcher (API and scrape paths)."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError
from franktheunicorn.data_access.github.issue_fetcher import (
    ISSUE_REF_PATTERN,
    IssueFetcher,
)


class TestIssueFetchViaAPI:
    def test_fetches_issue(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_api_json: dict[str, Any],
        issue_comments_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42",
            json=issue_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42/comments?per_page=5",
            json=issue_comments_api_json,
        )
        result = issue_fetcher.fetch_via_api("apache", "spark", 42)
        assert result.fetched_via == FetchMethod.API
        assert result.number == 42
        assert result.title == "Support mapInArrow for Spark Connect"
        assert result.state == "open"
        assert result.author == "huaxingao"
        assert "enhancement" in result.labels
        assert "spark-connect" in result.labels
        assert result.url == "https://github.com/apache/spark/issues/42"

    def test_parses_body(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_api_json: dict[str, Any],
        issue_comments_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42",
            json=issue_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42/comments?per_page=5",
            json=issue_comments_api_json,
        )
        result = issue_fetcher.fetch_via_api("apache", "spark", 42)
        assert "mapInArrow" in result.body

    def test_parses_comments(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_api_json: dict[str, Any],
        issue_comments_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42",
            json=issue_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42/comments?per_page=5",
            json=issue_comments_api_json,
        )
        result = issue_fetcher.fetch_via_api("apache", "spark", 42)
        assert len(result.comments) == 2
        assert result.comments[0].author == "dongjoon-hyun"
        assert "batching" in result.comments[0].body

    def test_no_comments_when_count_zero(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/1",
            json={
                "number": 1,
                "title": "Test",
                "state": "open",
                "body": "",
                "user": {"login": "test"},
                "labels": [],
                "html_url": "https://github.com/apache/spark/issues/1",
                "comments": 0,
            },
        )
        result = issue_fetcher.fetch_via_api("apache", "spark", 1)
        assert result.comments == []

    def test_404_raises_not_found(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/99999",
            status_code=404,
        )
        with pytest.raises(NotFoundError):
            issue_fetcher.fetch_via_api("apache", "spark", 99999)


class TestIssueFetchViaScrape:
    def test_fetches_issue(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/issues/42",
            text=issue_scrape_html,
        )
        result = issue_fetcher.fetch_via_scrape("apache", "spark", 42)
        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.number == 42
        assert result.title == "Support mapInArrow for Spark Connect"
        assert result.state == "open"
        assert result.author == "huaxingao"
        assert "enhancement" in result.labels
        assert "spark-connect" in result.labels

    def test_parses_body(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/issues/42",
            text=issue_scrape_html,
        )
        result = issue_fetcher.fetch_via_scrape("apache", "spark", 42)
        assert "mapInArrow" in result.body

    def test_parses_comments(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/issues/42",
            text=issue_scrape_html,
        )
        result = issue_fetcher.fetch_via_scrape("apache", "spark", 42)
        assert len(result.comments) == 2
        assert result.comments[0].author == "dongjoon-hyun"
        assert "batching" in result.comments[0].body

    def test_404_raises_not_found(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
    ) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/issues/99999",
            status_code=404,
        )
        with pytest.raises(NotFoundError):
            issue_fetcher.fetch_via_scrape("apache", "spark", 99999)


class TestFetchLinkedIssues:
    def test_parses_same_repo_references(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_api_json: dict[str, Any],
        issue_comments_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42",
            json=issue_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42/comments?per_page=5",
            json=issue_comments_api_json,
        )
        results = issue_fetcher.fetch_linked_issues("apache", "spark", "Fixes #42")
        assert len(results) == 1
        assert results[0].number == 42

    def test_parses_cross_repo_references(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_api_json: dict[str, Any],
        issue_comments_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/other/project/issues/99",
            json={
                **issue_api_json,
                "number": 99,
                "comments": 0,
            },
        )
        results = issue_fetcher.fetch_linked_issues("apache", "spark", "See other/project#99")
        assert len(results) == 1
        assert results[0].number == 99

    def test_deduplicates_references(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_api_json: dict[str, Any],
        issue_comments_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42",
            json=issue_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42/comments?per_page=5",
            json=issue_comments_api_json,
        )
        results = issue_fetcher.fetch_linked_issues("apache", "spark", "Fixes #42, also #42")
        assert len(results) == 1

    def test_no_references(
        self,
        issue_fetcher: IssueFetcher,
    ) -> None:
        results = issue_fetcher.fetch_linked_issues("apache", "spark", "No issue references here")
        assert results == []

    def test_handles_fetch_failure_gracefully(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/999",
            status_code=404,
        )
        results = issue_fetcher.fetch_linked_issues("apache", "spark", "See #999")
        assert results == []


class TestFetchRelatedIssues:
    def test_searches_issues(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
        issue_search_api_json: dict[str, Any],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/search/issues?q=mapInArrow%2Brepo%3Aapache%2Fspark%2Bis%3Aissue",
            json=issue_search_api_json,
        )
        results = issue_fetcher.fetch_related_issues("apache", "spark", "mapInArrow")
        assert len(results) == 2
        assert results[0].title == "Support mapInArrow for Spark Connect"
        assert results[1].title == "mapInArrow performance regression"

    def test_empty_search_results(
        self,
        httpx_mock: HTTPXMock,
        issue_fetcher: IssueFetcher,
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/search/issues?q=nonexistent%2Brepo%3Aapache%2Fspark%2Bis%3Aissue",
            json={"total_count": 0, "incomplete_results": False, "items": []},
        )
        results = issue_fetcher.fetch_related_issues("apache", "spark", "nonexistent")
        assert results == []


class TestIssueRefPattern:
    def test_same_repo_ref(self) -> None:
        matches = ISSUE_REF_PATTERN.findall("Fixes #123")
        assert ("", "", "123") in matches

    def test_cross_repo_ref(self) -> None:
        matches = ISSUE_REF_PATTERN.findall("See org/repo#456")
        assert ("org", "repo", "456") in matches

    def test_multiple_refs(self) -> None:
        text = "Fixes #123, see also other/project#456"
        matches = ISSUE_REF_PATTERN.findall(text)
        assert len(matches) == 2

    def test_no_refs(self) -> None:
        matches = ISSUE_REF_PATTERN.findall("No references here")
        assert matches == []
