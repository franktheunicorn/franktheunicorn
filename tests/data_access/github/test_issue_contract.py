"""Contract tests verifying API and scrape paths produce compatible results."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.github.issue_fetcher import IssueFetcher
from franktheunicorn.data_access.github.issue_types import GitHubIssueResult


@pytest.fixture(params=["api", "scrape"])
def issue_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    issue_fetcher: IssueFetcher,
    issue_api_json: dict[str, Any],
    issue_comments_api_json: list[dict[str, Any]],
    issue_scrape_html: str,
) -> GitHubIssueResult:
    if request.param == "api":
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42",
            json=issue_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/issues/42/comments?per_page=5",
            json=issue_comments_api_json,
        )
        return issue_fetcher.fetch_via_api("apache", "spark", 42)
    httpx_mock.add_response(
        url="https://github.com/apache/spark/issues/42",
        text=issue_scrape_html,
    )
    return issue_fetcher.fetch_via_scrape("apache", "spark", 42)


class TestIssueContract:
    def test_number(self, issue_result: GitHubIssueResult) -> None:
        assert issue_result.number == 42

    def test_title(self, issue_result: GitHubIssueResult) -> None:
        assert "mapInArrow" in issue_result.title

    def test_state(self, issue_result: GitHubIssueResult) -> None:
        assert issue_result.state == "open"

    def test_author(self, issue_result: GitHubIssueResult) -> None:
        assert issue_result.author == "huaxingao"

    def test_has_labels(self, issue_result: GitHubIssueResult) -> None:
        assert "enhancement" in issue_result.labels

    def test_has_body(self, issue_result: GitHubIssueResult) -> None:
        assert len(issue_result.body) > 0
        assert "mapInArrow" in issue_result.body

    def test_has_comments(self, issue_result: GitHubIssueResult) -> None:
        assert len(issue_result.comments) >= 1

    def test_to_prompt_context(self, issue_result: GitHubIssueResult) -> None:
        ctx = issue_result.to_prompt_context()
        assert "#42" in ctx
        assert "mapInArrow" in ctx

    def test_to_cache_dict(self, issue_result: GitHubIssueResult) -> None:
        d = issue_result.to_cache_dict()
        assert d["number"] == 42
        assert isinstance(d["labels"], list)
        assert isinstance(d["comments"], list)
