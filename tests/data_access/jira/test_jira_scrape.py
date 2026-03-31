"""Tests for JIRA HTML scrape fetch path."""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.jira.fetcher import JiraFetcher


class TestJiraScrapeFetch:
    def test_fetches_ticket(
        self, httpx_mock: HTTPXMock, jira_fetcher: JiraFetcher, ticket_scrape_html: str
    ) -> None:
        httpx_mock.add_response(
            url="https://issues.apache.org/jira/browse/SPARK-12345",
            text=ticket_scrape_html,
        )
        result = jira_fetcher.fetch_via_scrape("https://issues.apache.org/jira", "SPARK-12345")
        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.ticket_id == "SPARK-12345"
        assert result.summary == "Add DataFrame.mapInArrow for Connect"
        assert result.status == "Open"
        assert result.priority == "Major"
        assert result.assignee == "Huaxin Gao"

    def test_parses_description(
        self, httpx_mock: HTTPXMock, jira_fetcher: JiraFetcher, ticket_scrape_html: str
    ) -> None:
        httpx_mock.add_response(
            url="https://issues.apache.org/jira/browse/SPARK-12345",
            text=ticket_scrape_html,
        )
        result = jira_fetcher.fetch_via_scrape("https://issues.apache.org/jira", "SPARK-12345")
        assert "mapInArrow" in result.description

    def test_parses_comments(
        self, httpx_mock: HTTPXMock, jira_fetcher: JiraFetcher, ticket_scrape_html: str
    ) -> None:
        httpx_mock.add_response(
            url="https://issues.apache.org/jira/browse/SPARK-12345",
            text=ticket_scrape_html,
        )
        result = jira_fetcher.fetch_via_scrape("https://issues.apache.org/jira", "SPARK-12345")
        assert len(result.recent_comments) == 2
        assert result.recent_comments[0].author == "Dongjoon Hyun"
        assert "batching" in result.recent_comments[0].body
