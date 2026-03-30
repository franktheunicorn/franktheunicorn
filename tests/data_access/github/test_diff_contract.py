"""Contract tests: DiffFetcher API and scrape paths produce identical structure."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher
from franktheunicorn.data_access.github.types import PRDiff, PRFileChange


@pytest.fixture(params=["api", "scrape"])
def diff_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    diff_fetcher: DiffFetcher,
    pr_diff_text: str,
) -> PRDiff:
    if request.param == "api":
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", text=pr_diff_text
        )
        return diff_fetcher.fetch_via_api("apache", "spark", 42)
    httpx_mock.add_response(url="https://github.com/apache/spark/pull/42.diff", text=pr_diff_text)
    return diff_fetcher.fetch_via_scrape("apache", "spark", 42)


class TestDiffContract:
    def test_returns_pr_diff(self, diff_result: PRDiff) -> None:
        assert isinstance(diff_result, PRDiff)
        assert diff_result.pr_number == 42
        assert len(diff_result.raw_diff) > 0
        assert diff_result.fetched_via in (FetchMethod.API, FetchMethod.SCRAPE)

    def test_files_structure(self, diff_result: PRDiff) -> None:
        assert len(diff_result.files) == 2
        filenames = [f.filename for f in diff_result.files]
        assert "core/src/test/scala/SchedulerSuite.scala" in filenames
        assert "docs/TESTING.md" in filenames
        for f in diff_result.files:
            assert isinstance(f, PRFileChange)
            assert f.status in {"modified", "added", "removed", "renamed"}
