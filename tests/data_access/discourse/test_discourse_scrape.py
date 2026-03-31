"""Tests for Discourse HTML scrape fetch path."""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.discourse.fetcher import DiscourseFetcher


class TestDiscourseScrapeFetch:
    def test_fetches_search_results(
        self,
        httpx_mock: HTTPXMock,
        discourse_fetcher: DiscourseFetcher,
        search_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://discuss.example.org/search?q=mapInArrow",
            text=search_scrape_html,
        )
        result = discourse_fetcher.fetch_via_scrape("https://discuss.example.org", "mapInArrow")
        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.query == "mapInArrow"
        assert len(result.posts) == 2

    def test_parses_post_fields(
        self,
        httpx_mock: HTTPXMock,
        discourse_fetcher: DiscourseFetcher,
        search_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://discuss.example.org/search?q=mapInArrow",
            text=search_scrape_html,
        )
        result = discourse_fetcher.fetch_via_scrape("https://discuss.example.org", "mapInArrow")
        post = result.posts[0]
        assert post.title == "RFC: mapInArrow for Spark Connect"
        assert post.author == "holdenk"
        assert "mapInArrow" in post.excerpt
        assert post.date == "2025-11-15T10:30:00Z"
        assert post.category == "Development"

    def test_constructs_full_urls(
        self,
        httpx_mock: HTTPXMock,
        discourse_fetcher: DiscourseFetcher,
        search_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://discuss.example.org/search?q=mapInArrow",
            text=search_scrape_html,
        )
        result = discourse_fetcher.fetch_via_scrape("https://discuss.example.org", "mapInArrow")
        assert result.posts[0].url == (
            "https://discuss.example.org/t/rfc-mapinarrow-for-spark-connect/501"
        )

    def test_empty_html(
        self,
        httpx_mock: HTTPXMock,
        discourse_fetcher: DiscourseFetcher,
    ) -> None:
        httpx_mock.add_response(
            url="https://discuss.example.org/search?q=nonexistent",
            text="<html><body><div class='search-results'></div></body></html>",
        )
        result = discourse_fetcher.fetch_via_scrape("https://discuss.example.org", "nonexistent")
        assert result.posts == []
