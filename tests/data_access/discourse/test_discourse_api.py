"""Tests for Discourse REST API fetch path."""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.discourse.fetcher import DiscourseFetcher


class TestDiscourseAPIFetch:
    def test_fetches_search_results(
        self,
        httpx_mock: HTTPXMock,
        discourse_fetcher: DiscourseFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(
            url="https://discuss.example.org/search.json?q=mapInArrow",
            json=search_api_json,
        )
        result = discourse_fetcher.fetch_via_api("https://discuss.example.org", "mapInArrow")
        assert result.fetched_via == FetchMethod.API
        assert result.query == "mapInArrow"
        assert result.base_url == "https://discuss.example.org"
        assert len(result.posts) == 2

    def test_parses_post_fields(
        self,
        httpx_mock: HTTPXMock,
        discourse_fetcher: DiscourseFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(
            url="https://discuss.example.org/search.json?q=mapInArrow",
            json=search_api_json,
        )
        result = discourse_fetcher.fetch_via_api("https://discuss.example.org", "mapInArrow")
        post = result.posts[0]
        assert post.title == "RFC: mapInArrow for Spark Connect"
        assert post.author == "holdenk"
        assert "mapInArrow" in post.excerpt
        assert post.date == "2025-11-15T10:30:00Z"
        assert post.category == "Development"
        assert "/t/rfc-mapinarrow-for-spark-connect/501" in post.url

    def test_constructs_topic_urls(
        self,
        httpx_mock: HTTPXMock,
        discourse_fetcher: DiscourseFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(
            url="https://discuss.example.org/search.json?q=mapInArrow",
            json=search_api_json,
        )
        result = discourse_fetcher.fetch_via_api("https://discuss.example.org", "mapInArrow")
        assert result.posts[0].url == (
            "https://discuss.example.org/t/rfc-mapinarrow-for-spark-connect/501"
        )
        assert result.posts[1].url == (
            "https://discuss.example.org/t/arrow-batching-performance-results/502"
        )

    def test_empty_response(
        self,
        httpx_mock: HTTPXMock,
        discourse_fetcher: DiscourseFetcher,
    ) -> None:
        httpx_mock.add_response(
            url="https://discuss.example.org/search.json?q=nonexistent",
            json={"posts": [], "topics": []},
        )
        result = discourse_fetcher.fetch_via_api("https://discuss.example.org", "nonexistent")
        assert result.posts == []
        assert result.query == "nonexistent"

    def test_strips_trailing_slash(
        self,
        httpx_mock: HTTPXMock,
        discourse_fetcher: DiscourseFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(
            url="https://discuss.example.org/search.json?q=test",
            json=search_api_json,
        )
        result = discourse_fetcher.fetch_via_api("https://discuss.example.org/", "test")
        assert result.base_url == "https://discuss.example.org"
