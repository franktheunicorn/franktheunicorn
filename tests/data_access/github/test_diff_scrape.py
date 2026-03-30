"""Tests for DiffFetcherScrape."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError
from franktheunicorn.data_access.github.diff_fetcher import DiffFetcherScrape
from franktheunicorn.data_access.github.types import PRDiff


class TestDiffFetcherScrape:
    def test_fetches_diff_from_public_url(self, httpx_mock: HTTPXMock, pr_diff_text: str) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42.diff",
            text=pr_diff_text,
        )
        client = httpx.Client()
        fetcher = DiffFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        assert isinstance(result, PRDiff)
        assert result.pr_number == 42
        assert result.fetched_via == FetchMethod.SCRAPE
        assert len(result.files) == 2
        assert result.files[0].filename == "core/src/test/scala/SchedulerSuite.scala"
        assert result.files[1].filename == "docs/TESTING.md"
        client.close()

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/999.diff",
            status_code=404,
        )
        client = httpx.Client()
        fetcher = DiffFetcherScrape(client=client)

        with pytest.raises(NotFoundError) as exc_info:
            fetcher.fetch_via_scrape("apache", "spark", 999)

        assert exc_info.value.method == FetchMethod.SCRAPE
        client.close()

    def test_fetch_method_goes_directly_to_scrape(
        self, httpx_mock: HTTPXMock, pr_diff_text: str
    ) -> None:
        """DiffFetcherScrape.fetch() should skip API and go to scrape."""
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42.diff",
            text=pr_diff_text,
        )
        client = httpx.Client()
        fetcher = DiffFetcherScrape(client=client)
        result = fetcher.fetch("apache", "spark", 42)

        assert result.fetched_via == FetchMethod.SCRAPE
        client.close()

    def test_parses_file_statuses(self, httpx_mock: HTTPXMock) -> None:
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
            url="https://github.com/apache/spark/pull/99.diff",
            text=diff_with_new_file,
        )
        client = httpx.Client()
        fetcher = DiffFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 99)

        assert len(result.files) == 1
        assert result.files[0].filename == "new_file.py"
        assert result.files[0].status == "added"
        assert result.files[0].additions >= 3
        client.close()
