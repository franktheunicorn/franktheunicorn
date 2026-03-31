"""Tests for JIRA REST API fetch path."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.jira.fetcher import JiraFetcher


class TestJiraAPIFetch:
    def test_fetches_ticket(
        self, httpx_mock: HTTPXMock, jira_fetcher: JiraFetcher, ticket_api_json: dict
    ) -> None:
        httpx_mock.add_response(
            url="https://issues.apache.org/jira/rest/api/2/issue/SPARK-12345",
            json=ticket_api_json,
        )
        result = jira_fetcher.fetch_via_api("https://issues.apache.org/jira", "SPARK-12345")
        assert result.fetched_via == FetchMethod.API
        assert result.ticket_id == "SPARK-12345"
        assert result.summary == "Add DataFrame.mapInArrow for Connect"
        assert result.status == "Open"
        assert result.priority == "Major"
        assert result.assignee == "Huaxin Gao"
        assert result.issue_type == "Improvement"
        assert result.project == "SPARK"

    def test_parses_description(
        self, httpx_mock: HTTPXMock, jira_fetcher: JiraFetcher, ticket_api_json: dict
    ) -> None:
        httpx_mock.add_response(
            url="https://issues.apache.org/jira/rest/api/2/issue/SPARK-12345",
            json=ticket_api_json,
        )
        result = jira_fetcher.fetch_via_api("https://issues.apache.org/jira", "SPARK-12345")
        assert "mapInArrow" in result.description

    def test_parses_comments(
        self, httpx_mock: HTTPXMock, jira_fetcher: JiraFetcher, ticket_api_json: dict
    ) -> None:
        httpx_mock.add_response(
            url="https://issues.apache.org/jira/rest/api/2/issue/SPARK-12345",
            json=ticket_api_json,
        )
        result = jira_fetcher.fetch_via_api("https://issues.apache.org/jira", "SPARK-12345")
        assert len(result.recent_comments) == 2
        assert result.recent_comments[0].author == "Dongjoon Hyun"
        assert "batching" in result.recent_comments[0].body

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock, jira_fetcher: JiraFetcher) -> None:
        from franktheunicorn.data_access.base import NotFoundError

        httpx_mock.add_response(
            url="https://issues.apache.org/jira/rest/api/2/issue/SPARK-99999",
            status_code=404,
        )
        with pytest.raises(NotFoundError):
            jira_fetcher.fetch_via_api("https://issues.apache.org/jira", "SPARK-99999")

    def test_empty_fields_handled(self, httpx_mock: HTTPXMock, jira_fetcher: JiraFetcher) -> None:
        httpx_mock.add_response(
            url="https://issues.apache.org/jira/rest/api/2/issue/SPARK-1",
            json={"key": "SPARK-1", "fields": {}},
        )
        result = jira_fetcher.fetch_via_api("https://issues.apache.org/jira", "SPARK-1")
        assert result.summary == ""
        assert result.assignee == ""
        assert result.recent_comments == []
