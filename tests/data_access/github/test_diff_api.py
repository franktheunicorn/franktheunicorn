"""Tests for DiffFetcherAPI."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError, RateLimitError
from franktheunicorn.data_access.github.diff_fetcher import DiffFetcherAPI
from franktheunicorn.data_access.github.types import PRDiff


class TestDiffFetcherAPI:
    def test_fetches_diff_and_parses_files(self, httpx_mock: HTTPXMock, pr_diff_text: str) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            text=pr_diff_text,
        )
        client = httpx.Client()
        fetcher = DiffFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)

        assert isinstance(result, PRDiff)
        assert result.pr_number == 42
        assert result.fetched_via == FetchMethod.API
        assert len(result.files) == 2
        assert result.files[0].filename == "core/src/test/scala/SchedulerSuite.scala"
        assert result.files[1].filename == "docs/TESTING.md"
        assert result.raw_diff == pr_diff_text
        client.close()

    def test_file_addition_counts(self, httpx_mock: HTTPXMock, pr_diff_text: str) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            text=pr_diff_text,
        )
        client = httpx.Client()
        fetcher = DiffFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)

        # First file: 3 additions, 1 deletion
        assert result.files[0].additions >= 1
        assert result.files[0].deletions >= 1
        assert result.files[0].status == "modified"
        client.close()

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/999",
            status_code=404,
        )
        client = httpx.Client()
        fetcher = DiffFetcherAPI(client=client)

        with pytest.raises(NotFoundError) as exc_info:
            fetcher.fetch_via_api("apache", "spark", 999)

        assert exc_info.value.status_code == 404
        assert exc_info.value.method == FetchMethod.API
        client.close()

    def test_403_raises_rate_limit(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            status_code=403,
        )
        client = httpx.Client()
        fetcher = DiffFetcherAPI(client=client)

        with pytest.raises(RateLimitError):
            fetcher.fetch_via_api("apache", "spark", 42)
        client.close()

    def test_429_raises_rate_limit(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            status_code=429,
        )
        client = httpx.Client()
        fetcher = DiffFetcherAPI(client=client)

        with pytest.raises(RateLimitError):
            fetcher.fetch_via_api("apache", "spark", 42)
        client.close()

    def test_fetch_falls_back_to_scrape_on_rate_limit(
        self, httpx_mock: HTTPXMock, pr_diff_text: str
    ) -> None:
        """The unified fetch() should fall back to scrape on 403."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            status_code=403,
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42.diff",
            text=pr_diff_text,
        )
        client = httpx.Client()
        fetcher = DiffFetcherAPI(client=client)
        result = fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRDiff)
        assert result.fetched_via == FetchMethod.SCRAPE
        assert len(result.files) == 2
        client.close()
