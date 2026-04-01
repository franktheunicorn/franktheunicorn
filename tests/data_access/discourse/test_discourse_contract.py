"""Contract tests verifying API and scrape paths produce compatible results."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.discourse.fetcher import DiscourseFetcher
from franktheunicorn.data_access.discourse.types import DiscourseSearchResult

BASE_URL = "https://discuss.example.org"


@pytest.fixture(params=["api", "scrape"])
def discourse_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    discourse_fetcher: DiscourseFetcher,
    search_api_json: dict,
    search_scrape_html: str,
) -> DiscourseSearchResult:
    if request.param == "api":
        httpx_mock.add_response(
            url=f"{BASE_URL}/search.json?q=mapInArrow",
            json=search_api_json,
        )
        return discourse_fetcher.fetch_via_api(BASE_URL, "mapInArrow")
    httpx_mock.add_response(
        url=f"{BASE_URL}/search?q=mapInArrow",
        text=search_scrape_html,
    )
    return discourse_fetcher.fetch_via_scrape(BASE_URL, "mapInArrow")


class TestDiscourseContract:
    def test_query(self, discourse_result: DiscourseSearchResult) -> None:
        assert discourse_result.query == "mapInArrow"

    def test_has_posts(self, discourse_result: DiscourseSearchResult) -> None:
        assert len(discourse_result.posts) == 2

    def test_first_post_title(self, discourse_result: DiscourseSearchResult) -> None:
        assert "mapInArrow" in discourse_result.posts[0].title

    def test_first_post_author(self, discourse_result: DiscourseSearchResult) -> None:
        assert discourse_result.posts[0].author == "holdenk"

    def test_first_post_has_excerpt(self, discourse_result: DiscourseSearchResult) -> None:
        assert len(discourse_result.posts[0].excerpt) > 0

    def test_first_post_has_date(self, discourse_result: DiscourseSearchResult) -> None:
        assert discourse_result.posts[0].date == "2025-11-15T10:30:00Z"

    def test_first_post_category(self, discourse_result: DiscourseSearchResult) -> None:
        assert discourse_result.posts[0].category == "Development"

    def test_to_prompt_context(self, discourse_result: DiscourseSearchResult) -> None:
        ctx = discourse_result.to_prompt_context()
        assert "mapInArrow" in ctx
        assert "holdenk" in ctx

    def test_to_cache_dict(self, discourse_result: DiscourseSearchResult) -> None:
        d = discourse_result.to_cache_dict()
        assert d["query"] == "mapInArrow"
        assert isinstance(d["posts"], list)
        assert len(d["posts"]) == 2
        assert d["posts"][0]["author"] == "holdenk"
