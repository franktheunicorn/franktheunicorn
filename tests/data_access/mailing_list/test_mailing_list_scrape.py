"""Tests for mailing list HTML scrape fetch path."""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.mailing_list.fetcher import MailingListFetcher

ARCHIVE_URL = "https://lists.apache.org/list.html?dev@spark.apache.org"


class TestMailingListScrapeFetch:
    def test_fetches_threads(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        archive_page_html: str,
    ) -> None:
        httpx_mock.add_response(url=ARCHIVE_URL, text=archive_page_html)
        result = ml_fetcher.fetch_via_scrape(ARCHIVE_URL, "mapInArrow")
        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.query == "mapInArrow"
        assert len(result.threads) == 2

    def test_parses_thread_fields(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        archive_page_html: str,
    ) -> None:
        httpx_mock.add_response(url=ARCHIVE_URL, text=archive_page_html)
        result = ml_fetcher.fetch_via_scrape(ARCHIVE_URL, "mapInArrow")
        thread = result.threads[0]
        assert "mapInArrow" in thread.subject
        assert thread.date == "2026-03-15"
        assert "Huaxin Gao" in thread.participants
        assert thread.url.endswith("/thread/abc123")

    def test_filters_by_query(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        archive_page_html: str,
    ) -> None:
        httpx_mock.add_response(url=ARCHIVE_URL, text=archive_page_html)
        result = ml_fetcher.fetch_via_scrape(ARCHIVE_URL, "mapInArrow")
        for thread in result.threads:
            assert "mapInArrow" in thread.subject

    def test_extracts_list_name_from_title(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
        archive_page_html: str,
    ) -> None:
        httpx_mock.add_response(url=ARCHIVE_URL, text=archive_page_html)
        result = ml_fetcher.fetch_via_scrape(ARCHIVE_URL, "mapInArrow")
        assert "spark.apache.org" in result.list_name

    def test_empty_page(
        self,
        httpx_mock: HTTPXMock,
        ml_fetcher: MailingListFetcher,
    ) -> None:
        httpx_mock.add_response(
            url=ARCHIVE_URL,
            text="<html><head><title>Empty</title></head><body></body></html>",
        )
        result = ml_fetcher.fetch_via_scrape(ARCHIVE_URL, "mapInArrow")
        assert result.threads == []
