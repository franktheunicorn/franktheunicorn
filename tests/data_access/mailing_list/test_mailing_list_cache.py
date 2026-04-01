"""Tests verifying cache behavior for mailing list fetcher."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.mailing_list.fetcher import MailingListFetcher

ARCHIVE_URL = "https://lists.apache.org/list.html?dev@spark.apache.org"
API_URL = (
    "https://lists.apache.org/api/stats.lua?list=dev&domain=spark.apache.org&d=lte1y&q=mapInArrow"
)


class TestMailingListCache:
    def test_cache_miss_fetches_from_api(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(url=API_URL, json=search_api_json)
        result = ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
        assert len(result.threads) == 2

    def test_cache_hit_skips_fetch(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        search_api_json: dict,
    ) -> None:
        # First call: populates cache.
        httpx_mock.add_response(url=API_URL, json=search_api_json)
        result1 = ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")

        # Second call: should use cache (no mock needed).
        result2 = ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
        assert result2.query == result1.query
        assert len(result2.threads) == len(result1.threads)

    def test_cache_hit_scrape_path(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        archive_page_html: str,
    ) -> None:
        # First call: populates cache.
        httpx_mock.add_response(url=ARCHIVE_URL, text=archive_page_html)
        result1 = ml_fetcher.fetch_via_scrape(ARCHIVE_URL, "mapInArrow")

        # Second call: should use cache.
        result2 = ml_fetcher.fetch_via_scrape(ARCHIVE_URL, "mapInArrow")
        assert result2.query == result1.query
        assert len(result2.threads) == len(result1.threads)

    def test_cache_ttl_expiry(
        self,
        httpx_mock: HTTPXMock,
        http_client: object,
        tmp_path: Path,
        search_api_json: dict,
    ) -> None:
        # Use a very short TTL.
        short_cache = FileCache("mailing_list", cache_dir=tmp_path, ttl_seconds=1)
        fetcher = MailingListFetcher(
            client=http_client,  # type: ignore[arg-type]
            delay_seconds=0.0,
            cache=short_cache,
        )

        # First fetch.
        httpx_mock.add_response(url=API_URL, json=search_api_json)
        result1 = fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
        assert len(result1.threads) == 2

        # Simulate time passing beyond TTL.
        with patch("franktheunicorn.data_access.cache.time") as mock_time:
            mock_time.time.return_value = time.time() + 10
            # Cache should be expired; needs a new HTTP response.
            httpx_mock.add_response(url=API_URL, json=search_api_json)
            result2 = fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
            assert len(result2.threads) == 2

    def test_different_queries_have_separate_cache(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        search_api_json: dict,
    ) -> None:
        api_url_other = (
            "https://lists.apache.org/api/stats.lua"
            "?list=dev&domain=spark.apache.org&d=lte1y&q=otherQuery"
        )
        httpx_mock.add_response(url=API_URL, json=search_api_json)
        httpx_mock.add_response(
            url=api_url_other,
            json={"emails": [], "thread": [], "total": 0},
        )

        result1 = ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
        result2 = ml_fetcher.fetch_via_api(ARCHIVE_URL, "otherQuery")

        assert len(result1.threads) == 2
        assert len(result2.threads) == 0
