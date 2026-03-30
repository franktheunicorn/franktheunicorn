"""Tests for DiffFetcher (API + scrape paths)."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError, RateLimitError
from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher
from franktheunicorn.data_access.github.types import PRDiff


class TestDiffFetcherAPI:
    def test_fetches_and_parses(
        self, httpx_mock: HTTPXMock, diff_fetcher: DiffFetcher, pr_diff_text: str
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", text=pr_diff_text
        )
        result = diff_fetcher.fetch_via_api("apache", "spark", 42)

        assert isinstance(result, PRDiff)
        assert result.pr_number == 42
        assert result.fetched_via == FetchMethod.API
        assert len(result.files) == 2
        assert result.files[0].filename == "core/src/test/scala/SchedulerSuite.scala"
        assert result.files[1].filename == "docs/TESTING.md"
        assert result.files[0].additions >= 1
        assert result.files[0].deletions >= 1
        assert result.files[0].status == "modified"

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock, diff_fetcher: DiffFetcher) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/999", status_code=404
        )
        with pytest.raises(NotFoundError) as exc_info:
            diff_fetcher.fetch_via_api("apache", "spark", 999)
        assert exc_info.value.status_code == 404

    def test_403_raises_rate_limit(self, httpx_mock: HTTPXMock, diff_fetcher: DiffFetcher) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", status_code=403
        )
        with pytest.raises(RateLimitError):
            diff_fetcher.fetch_via_api("apache", "spark", 42)

    def test_429_raises_rate_limit(self, httpx_mock: HTTPXMock, diff_fetcher: DiffFetcher) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", status_code=429
        )
        with pytest.raises(RateLimitError):
            diff_fetcher.fetch_via_api("apache", "spark", 42)


class TestDiffFetcherScrape:
    def test_fetches_from_public_url(
        self, httpx_mock: HTTPXMock, diff_fetcher: DiffFetcher, pr_diff_text: str
    ) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42.diff", text=pr_diff_text
        )
        result = diff_fetcher.fetch_via_scrape("apache", "spark", 42)

        assert isinstance(result, PRDiff)
        assert result.pr_number == 42
        assert result.fetched_via == FetchMethod.SCRAPE
        assert len(result.files) == 2

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock, diff_fetcher: DiffFetcher) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/999.diff", status_code=404
        )
        with pytest.raises(NotFoundError):
            diff_fetcher.fetch_via_scrape("apache", "spark", 999)

    def test_new_file_detected_as_added(
        self, httpx_mock: HTTPXMock, diff_fetcher: DiffFetcher
    ) -> None:
        diff_with_new_file = (
            "diff --git a/new_file.py b/new_file.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new_file.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+# New file\n"
            "+def hello():\n"
            "+    pass\n"
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/99.diff", text=diff_with_new_file
        )
        result = diff_fetcher.fetch_via_scrape("apache", "spark", 99)

        assert len(result.files) == 1
        assert result.files[0].status == "added"
        assert result.files[0].additions >= 3


class TestDiffFetcherFallback:
    def test_fetch_falls_back_to_scrape_on_403(
        self, httpx_mock: HTTPXMock, diff_fetcher: DiffFetcher, pr_diff_text: str
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", status_code=403
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42.diff", text=pr_diff_text
        )
        result = diff_fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRDiff)
        assert result.fetched_via == FetchMethod.SCRAPE
        assert len(result.files) == 2
