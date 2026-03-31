"""Contract tests verifying API and scrape paths produce compatible results."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.mailing_list.fetcher import (
    MailingListFetcher,
    _parse_archive_url,
)
from franktheunicorn.data_access.mailing_list.types import MailingListSearchResult

ARCHIVE_URL = "https://lists.apache.org/list.html?dev@spark.apache.org"
API_URL = (
    "https://lists.apache.org/api/stats.lua?list=dev&domain=spark.apache.org&d=lte1y&q=mapInArrow"
)


@pytest.fixture(params=["api", "scrape"])
def ml_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    ml_fetcher: MailingListFetcher,
    search_api_json: dict,
    archive_page_html: str,
) -> MailingListSearchResult:
    if request.param == "api":
        httpx_mock.add_response(url=API_URL, json=search_api_json)
        return ml_fetcher.fetch_via_api(ARCHIVE_URL, "mapInArrow")
    httpx_mock.add_response(url=ARCHIVE_URL, text=archive_page_html)
    return ml_fetcher.fetch_via_scrape(ARCHIVE_URL, "mapInArrow")


class TestMailingListContract:
    def test_query(self, ml_result: MailingListSearchResult) -> None:
        assert ml_result.query == "mapInArrow"

    def test_has_threads(self, ml_result: MailingListSearchResult) -> None:
        assert len(ml_result.threads) >= 1

    def test_threads_match_query(self, ml_result: MailingListSearchResult) -> None:
        for thread in ml_result.threads:
            assert "mapInArrow" in thread.subject

    def test_threads_have_subject(self, ml_result: MailingListSearchResult) -> None:
        for thread in ml_result.threads:
            assert len(thread.subject) > 0

    def test_threads_have_participants(self, ml_result: MailingListSearchResult) -> None:
        for thread in ml_result.threads:
            assert len(thread.participants) >= 1

    def test_threads_have_date(self, ml_result: MailingListSearchResult) -> None:
        for thread in ml_result.threads:
            assert len(thread.date) > 0

    def test_to_prompt_context(self, ml_result: MailingListSearchResult) -> None:
        ctx = ml_result.to_prompt_context()
        assert "mapInArrow" in ctx

    def test_to_cache_dict(self, ml_result: MailingListSearchResult) -> None:
        d = ml_result.to_cache_dict()
        assert d["query"] == "mapInArrow"
        assert isinstance(d["threads"], list)
        assert len(d["threads"]) >= 1

    def test_thread_to_prompt_context(self, ml_result: MailingListSearchResult) -> None:
        thread = ml_result.threads[0]
        ctx = thread.to_prompt_context()
        assert "mapInArrow" in ctx

    def test_thread_to_cache_dict(self, ml_result: MailingListSearchResult) -> None:
        thread = ml_result.threads[0]
        d = thread.to_cache_dict()
        assert "mapInArrow" in d["subject"]


class TestParseArchiveUrl:
    def test_standard_url(self) -> None:
        list_name, domain = _parse_archive_url(
            "https://lists.apache.org/list.html?dev@spark.apache.org"
        )
        assert list_name == "dev"
        assert domain == "spark.apache.org"

    def test_user_list(self) -> None:
        list_name, domain = _parse_archive_url(
            "https://lists.apache.org/list.html?user@kafka.apache.org"
        )
        assert list_name == "user"
        assert domain == "kafka.apache.org"

    def test_fallback_for_unknown_format(self) -> None:
        list_name, domain = _parse_archive_url("https://example.com/archive")
        assert list_name == "dev"
        assert domain == "spark.apache.org"
