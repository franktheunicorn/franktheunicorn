"""Tests for mailing list API fetch path."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError
from franktheunicorn.data_access.mailing_list.fetcher import MailingListFetcher

ARCHIVE_URL = "https://lists.apache.org/list.html?dev@spark.apache.org"
API_URL = (
    "https://lists.apache.org/api/stats.lua?list=dev&domain=spark.apache.org&d=lte1y&q=mapInArrow"
)


class TestMailingListAPIFetch:
    def test_fetches_threads(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(url=API_URL, json=search_api_json)
        result = ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
        assert result.fetched_via == FetchMethod.API
        assert result.query == "mapInArrow"
        assert result.list_name == "dev@spark.apache.org"
        assert len(result.threads) == 2

    def test_parses_thread_fields(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(url=API_URL, json=search_api_json)
        result = ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
        thread = result.threads[0]
        assert "mapInArrow" in thread.subject
        assert thread.date == "2026-03-15T10:00:00Z"
        assert "Huaxin Gao" in thread.participants
        assert len(thread.snippet) > 0
        assert thread.url == "https://lists.apache.org/thread/abc123"
        assert thread.list_name == "dev@spark.apache.org"

    def test_filters_by_query(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(url=API_URL, json=search_api_json)
        result = ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
        # The VOTE thread should be excluded (doesn't match query).
        for thread in result.threads:
            assert "mapInArrow" in thread.subject

    def test_404_raises_not_found(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
    ) -> None:
        httpx_mock.add_response(url=API_URL, status_code=404)
        with pytest.raises(NotFoundError):
            ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")

    def test_empty_response(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
    ) -> None:
        httpx_mock.add_response(
            url=API_URL,
            json={"emails": [], "thread": [], "total": 0},
        )
        result = ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
        assert result.threads == []
        assert result.query == "mapInArrow"
