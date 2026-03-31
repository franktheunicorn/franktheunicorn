"""Dual-path Discourse forum search fetcher.

API path: Discourse search API ``GET {base_url}/search.json?q={query}``
Scrape path: Parse search results HTML from ``{base_url}/search?q={query}``

Both paths return a ``DiscourseSearchResult`` with the same fields.
"""

from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    DataFetcher,
    FetchMethod,
)
from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.discourse.types import (
    DiscoursePost,
    DiscourseSearchResult,
)

logger = logging.getLogger(__name__)

_cache = FileCache("discourse")


class DiscourseFetcher(DataFetcher[DiscourseSearchResult]):
    """Fetches Discourse search results via REST API or HTML scrape."""

    def fetch_via_api(  # type: ignore[override]
        self,
        base_url: str,
        query: str,
        timeout_seconds: int = 30,
    ) -> DiscourseSearchResult:
        """Search Discourse via the JSON API."""
        base_url = base_url.rstrip("/")

        cached = _cache.get(base_url, query)
        if cached is not None:
            logger.debug("Discourse API cache hit for %s q=%s", base_url, query)
            return _result_from_cache(cached.data, FetchMethod.API)

        url = f"{base_url}/search.json"
        response = self._client.get(
            url,
            params={"q": query},
            timeout=timeout_seconds,
        )
        response.raise_for_status()

        data: dict[str, Any] = response.json()
        result = _parse_api_response(data, query, base_url)

        _cache.put(base_url, query, data=result.to_cache_dict())
        return result

    def fetch_via_scrape(  # type: ignore[override]
        self,
        base_url: str,
        query: str,
        timeout_seconds: int = 30,
    ) -> DiscourseSearchResult:
        """Search Discourse by scraping the HTML search page."""
        base_url = base_url.rstrip("/")

        cached = _cache.get(base_url, query)
        if cached is not None:
            logger.debug("Discourse scrape cache hit for %s q=%s", base_url, query)
            return _result_from_cache(cached.data, FetchMethod.SCRAPE)

        url = f"{base_url}/search"
        response = self._client.get(
            url,
            params={"q": query},
            timeout=timeout_seconds,
        )
        response.raise_for_status()

        result = _parse_html(response.text, query, base_url)

        _cache.put(base_url, query, data=result.to_cache_dict())
        return result


def _parse_api_response(
    data: dict[str, Any],
    query: str,
    base_url: str,
) -> DiscourseSearchResult:
    """Parse Discourse search API JSON into a DiscourseSearchResult."""
    topics_by_id: dict[int, dict[str, Any]] = {}
    for topic in data.get("topics", []):
        topics_by_id[topic.get("id", 0)] = topic

    posts: list[DiscoursePost] = []
    for post_data in data.get("posts", []):
        topic_id = post_data.get("topic_id", 0)
        topic = topics_by_id.get(topic_id, {})

        slug = topic.get("slug", "")
        topic_url = f"{base_url}/t/{slug}/{topic_id}" if slug else ""

        posts.append(
            DiscoursePost(
                fetched_via=FetchMethod.API,
                title=topic.get("title", post_data.get("name", "")),
                url=topic_url,
                excerpt=post_data.get("blurb", ""),
                date=post_data.get("created_at", ""),
                author=post_data.get("username", ""),
                category=topic.get("category_name", ""),
            )
        )

    return DiscourseSearchResult(
        fetched_via=FetchMethod.API,
        posts=posts,
        query=query,
        base_url=base_url,
    )


def _parse_html(
    html: str,
    query: str,
    base_url: str,
) -> DiscourseSearchResult:
    """Parse Discourse search results HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    posts: list[DiscoursePost] = []

    result_entries = soup.select(".fps-result")
    for entry in result_entries:
        title = ""
        url = ""
        title_el = entry.select_one(".topic-title")
        if title_el:
            title = title_el.get_text(strip=True)
            link = title_el.find("a")
            if link and link.get("href"):
                href = link["href"]
                url = f"{base_url}{href}" if href.startswith("/") else href

        excerpt = ""
        excerpt_el = entry.select_one(".blurb")
        if excerpt_el:
            excerpt = excerpt_el.get_text(strip=True)

        author = ""
        author_el = entry.select_one(".author .username")
        if author_el:
            author = author_el.get_text(strip=True)

        date = ""
        date_el = entry.select_one("time")
        if date_el:
            date = date_el.get("datetime", "")

        category = ""
        cat_el = entry.select_one(".category-name")
        if cat_el:
            category = cat_el.get_text(strip=True)

        posts.append(
            DiscoursePost(
                fetched_via=FetchMethod.SCRAPE,
                title=title,
                url=url,
                excerpt=excerpt,
                date=date,
                author=author,
                category=category,
            )
        )

    return DiscourseSearchResult(
        fetched_via=FetchMethod.SCRAPE,
        posts=posts,
        query=query,
        base_url=base_url,
    )


def _result_from_cache(
    data: dict[str, Any],
    method: FetchMethod,
) -> DiscourseSearchResult:
    """Reconstruct a DiscourseSearchResult from cached dict."""
    posts = [
        DiscoursePost(
            fetched_via=method,
            title=p.get("title", ""),
            url=p.get("url", ""),
            excerpt=p.get("excerpt", ""),
            date=p.get("date", ""),
            author=p.get("author", ""),
            category=p.get("category", ""),
        )
        for p in data.get("posts", [])
    ]
    return DiscourseSearchResult(
        fetched_via=method,
        posts=posts,
        query=data.get("query", ""),
        base_url=data.get("base_url", ""),
    )
