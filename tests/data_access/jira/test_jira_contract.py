"""Contract tests verifying API and scrape paths produce compatible results."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.jira.fetcher import JiraFetcher, extract_ticket_ids
from franktheunicorn.data_access.jira.types import JiraTicketResult


@pytest.fixture(params=["api", "scrape"])
def jira_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    jira_fetcher: JiraFetcher,
    ticket_api_json: dict,
    ticket_scrape_html: str,
) -> JiraTicketResult:
    server = "https://issues.apache.org/jira"
    if request.param == "api":
        httpx_mock.add_response(
            url=f"{server}/rest/api/2/issue/SPARK-12345",
            json=ticket_api_json,
        )
        return jira_fetcher.fetch_via_api(server, "SPARK-12345")
    httpx_mock.add_response(
        url=f"{server}/browse/SPARK-12345",
        text=ticket_scrape_html,
    )
    return jira_fetcher.fetch_via_scrape(server, "SPARK-12345")


class TestJiraContract:
    def test_ticket_id(self, jira_result: JiraTicketResult) -> None:
        assert jira_result.ticket_id == "SPARK-12345"

    def test_summary(self, jira_result: JiraTicketResult) -> None:
        assert "mapInArrow" in jira_result.summary

    def test_status(self, jira_result: JiraTicketResult) -> None:
        assert jira_result.status == "Open"

    def test_has_comments(self, jira_result: JiraTicketResult) -> None:
        assert len(jira_result.recent_comments) >= 1

    def test_has_description(self, jira_result: JiraTicketResult) -> None:
        assert len(jira_result.description) > 0

    def test_to_prompt_context(self, jira_result: JiraTicketResult) -> None:
        ctx = jira_result.to_prompt_context()
        assert "SPARK-12345" in ctx
        assert "mapInArrow" in ctx

    def test_to_cache_dict(self, jira_result: JiraTicketResult) -> None:
        d = jira_result.to_cache_dict()
        assert d["ticket_id"] == "SPARK-12345"
        assert isinstance(d["recent_comments"], list)


class TestExtractTicketIds:
    def test_single_ticket(self) -> None:
        assert extract_ticket_ids("Fix for SPARK-12345") == ["SPARK-12345"]

    def test_multiple_tickets(self) -> None:
        text = "Implements SPARK-100 and SPARK-200, see also HADOOP-50"
        result = extract_ticket_ids(text)
        assert result == ["SPARK-100", "SPARK-200", "HADOOP-50"]

    def test_with_prefix_filter(self) -> None:
        text = "SPARK-100 and HADOOP-50"
        result = extract_ticket_ids(text, project_prefix="SPARK")
        assert result == ["SPARK-100"]

    def test_deduplication(self) -> None:
        text = "SPARK-100 is related to SPARK-100"
        assert extract_ticket_ids(text) == ["SPARK-100"]

    def test_no_matches(self) -> None:
        assert extract_ticket_ids("No tickets here") == []

    def test_in_pr_title(self) -> None:
        text = "[SPARK-54102] Add DataFrame.mapInArrow for Connect"
        assert extract_ticket_ids(text, "SPARK") == ["SPARK-54102"]
