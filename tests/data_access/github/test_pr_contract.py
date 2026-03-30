"""Contract tests: PRFetcherAPI and PRFetcherScrape produce compatible structure."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.github.pr_fetcher import PRFetcherAPI, PRFetcherScrape
from franktheunicorn.data_access.github.types import PRSummary


@pytest.fixture(params=["api", "scrape"])
def pr_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    pr_api_json: dict[str, Any],
    pr_files_api_json: list[dict[str, Any]],
    pr_scrape_html: str,
) -> PRSummary:
    """Fetch a PRSummary via either path."""
    client = httpx.Client()
    if request.param == "api":
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            json=pr_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        fetcher = PRFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)
    else:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        fetcher = PRFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

    client.close()
    return result


class TestPRContract:
    def test_returns_pr_summary(self, pr_result: PRSummary) -> None:
        assert isinstance(pr_result, PRSummary)

    def test_number_set(self, pr_result: PRSummary) -> None:
        assert pr_result.number == 42

    def test_title_present(self, pr_result: PRSummary) -> None:
        assert "Fix flaky test" in pr_result.title

    def test_author_present(self, pr_result: PRSummary) -> None:
        assert pr_result.author == "alice-dev"

    def test_state_is_valid(self, pr_result: PRSummary) -> None:
        assert pr_result.state in ("open", "closed", "merged")

    def test_fetched_via_is_set(self, pr_result: PRSummary) -> None:
        assert pr_result.fetched_via in (FetchMethod.API, FetchMethod.SCRAPE)

    def test_labels_contain_bug(self, pr_result: PRSummary) -> None:
        assert "bug" in pr_result.labels

    def test_body_not_empty(self, pr_result: PRSummary) -> None:
        assert len(pr_result.body) > 0
