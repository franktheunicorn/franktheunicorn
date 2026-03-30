"""Contract tests: PRFetcher API and scrape paths produce compatible structure."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.github.pr_fetcher import PRFetcher
from franktheunicorn.data_access.github.types import PRSummary


@pytest.fixture(params=["api", "scrape"])
def pr_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    pr_fetcher: PRFetcher,
    pr_api_json: dict[str, Any],
    pr_files_api_json: list[dict[str, Any]],
    pr_scrape_html: str,
) -> PRSummary:
    if request.param == "api":
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        return pr_fetcher.fetch_via_api("apache", "spark", 42)
    httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
    return pr_fetcher.fetch_via_scrape("apache", "spark", 42)


class TestPRContract:
    def test_core_fields(self, pr_result: PRSummary) -> None:
        assert isinstance(pr_result, PRSummary)
        assert pr_result.number == 42
        assert "Fix flaky test" in pr_result.title
        assert pr_result.author == "alice-dev"
        assert pr_result.state in ("open", "closed", "merged")
        assert pr_result.fetched_via in (FetchMethod.API, FetchMethod.SCRAPE)
        assert "bug" in pr_result.labels
        assert len(pr_result.body) > 0
