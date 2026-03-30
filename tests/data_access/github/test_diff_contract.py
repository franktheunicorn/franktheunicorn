"""Contract tests: DiffFetcherAPI and DiffFetcherScrape produce identical structure."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.github.diff_fetcher import DiffFetcherAPI, DiffFetcherScrape
from franktheunicorn.data_access.github.types import PRDiff, PRFileChange


@pytest.fixture(params=["api", "scrape"])
def diff_result(
    request: pytest.FixtureRequest, httpx_mock: HTTPXMock, pr_diff_text: str
) -> PRDiff:
    """Fetch a PRDiff via either path using the same underlying diff text."""
    client = httpx.Client()
    if request.param == "api":
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            text=pr_diff_text,
        )
        fetcher = DiffFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)
    else:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42.diff",
            text=pr_diff_text,
        )
        fetcher = DiffFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

    client.close()
    return result


class TestDiffContract:
    def test_returns_pr_diff(self, diff_result: PRDiff) -> None:
        assert isinstance(diff_result, PRDiff)

    def test_pr_number_set(self, diff_result: PRDiff) -> None:
        assert diff_result.pr_number == 42

    def test_raw_diff_present(self, diff_result: PRDiff) -> None:
        assert len(diff_result.raw_diff) > 0

    def test_files_are_pr_file_changes(self, diff_result: PRDiff) -> None:
        assert len(diff_result.files) == 2
        for f in diff_result.files:
            assert isinstance(f, PRFileChange)

    def test_filenames_match(self, diff_result: PRDiff) -> None:
        filenames = [f.filename for f in diff_result.files]
        assert "core/src/test/scala/SchedulerSuite.scala" in filenames
        assert "docs/TESTING.md" in filenames

    def test_statuses_are_valid(self, diff_result: PRDiff) -> None:
        valid = {"modified", "added", "removed", "renamed"}
        for f in diff_result.files:
            assert f.status in valid

    def test_fetched_via_is_set(self, diff_result: PRDiff) -> None:
        assert diff_result.fetched_via in (FetchMethod.API, FetchMethod.SCRAPE)
