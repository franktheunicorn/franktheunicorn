"""Dual-path mailing list archive fetcher.

API path: Apache lists.apache.org Lua search API
Scrape path: Parse pipermail/mbox HTML archive pages

Both paths return a ``MailingListSearchResult`` with the same fields.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    DataFetcher,
    FetchMethod,
)
from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.mailing_list.types import (
    MailingListSearchResult,
    MailingListThread,
)

logger = logging.getLogger(__name__)

DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 30
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


class MailingListFetcher(DataFetcher[MailingListSearchResult]):
    """Fetches mailing list threads via API or HTML scrape."""

    def __init__(
        self,
        *args: Any,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        cache: FileCache | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._delay_seconds = delay_seconds
        self._cache = cache or FileCache("mailing_list", ttl_seconds=CACHE_TTL_SECONDS)

    def fetch_via_api(  # type: ignore[override]
        self,
        archive_url: str,
        query: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> MailingListSearchResult:
        """Fetch mailing list threads via Apache lists.apache.org API.

        URL pattern:
        https://lists.apache.org/api/stats.lua?list=dev&domain=spark.apache.org&d=lte1y&q=<query>
        """
        cached = self._cache.get(archive_url, query, "api")
        if cached is not None:
            logger.debug("Cache hit for mailing list API query: %s", query)
            return self._result_from_cache(cached.data, FetchMethod.API)

        # Parse list name and domain from archive_url.
        # Expected format: https://lists.apache.org/list.html?dev@spark.apache.org
        list_name, domain = _parse_archive_url(archive_url)

        url = (
            f"https://lists.apache.org/api/stats.lua"
            f"?list={quote_plus(list_name)}"
            f"&domain={quote_plus(domain)}"
            f"&d=lte1y"
            f"&q={quote_plus(query)}"
        )

        time.sleep(self._delay_seconds)
        response = self._api_get(url)
        data: dict[str, Any] = response.json()
        result = self._parse_api_response(data, query, list_name=f"{list_name}@{domain}")

        self._cache.put(archive_url, query, "api", data=result.to_cache_dict())
        return result

    def fetch_via_scrape(  # type: ignore[override]
        self,
        archive_url: str,
        query: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> MailingListSearchResult:
        """Fetch mailing list threads by scraping pipermail HTML archive."""
        cached = self._cache.get(archive_url, query, "scrape")
        if cached is not None:
            logger.debug("Cache hit for mailing list scrape query: %s", query)
            return self._result_from_cache(cached.data, FetchMethod.SCRAPE)

        time.sleep(self._delay_seconds)
        response = self._scrape_get(archive_url)
        result = self._parse_html(response.text, query, archive_url=archive_url)

        self._cache.put(archive_url, query, "scrape", data=result.to_cache_dict())
        return result

    @staticmethod
    def _parse_api_response(
        data: dict[str, Any],
        query: str,
        list_name: str,
    ) -> MailingListSearchResult:
        """Parse Apache lists.apache.org API JSON into search results."""
        threads: list[MailingListThread] = []
        emails = data.get("emails", [])
        for email in emails:
            subject = email.get("subject", "")
            # Only include threads whose subject matches the query.
            if query.lower() not in subject.lower():
                continue
            participants: list[str] = []
            from_field = email.get("from", "")
            if from_field:
                participants.append(from_field)
            threads.append(
                MailingListThread(
                    fetched_via=FetchMethod.API,
                    subject=subject,
                    date=email.get("date", ""),
                    participants=participants,
                    snippet=email.get("body", "")[:500],
                    url=email.get("permalink", ""),
                    list_name=list_name,
                )
            )

        # Also check the "thread" key (some responses use this).
        thread_list = data.get("thread", [])
        for thread_data in thread_list:
            subject = thread_data.get("subject", "")
            if query.lower() not in subject.lower():
                continue
            participants = [
                a.get("name", a.get("email", "")) for a in thread_data.get("authors", [])
            ]
            threads.append(
                MailingListThread(
                    fetched_via=FetchMethod.API,
                    subject=subject,
                    date=thread_data.get("date", ""),
                    participants=participants,
                    snippet=thread_data.get("snippet", "")[:500],
                    url=thread_data.get("permalink", ""),
                    list_name=list_name,
                )
            )

        return MailingListSearchResult(
            fetched_via=FetchMethod.API,
            threads=threads,
            query=query,
            list_name=list_name,
        )

    @staticmethod
    def _parse_html(
        html: str,
        query: str,
        archive_url: str,
    ) -> MailingListSearchResult:
        """Parse pipermail/mbox archive HTML page into search results."""
        soup = BeautifulSoup(html, "html.parser")
        threads: list[MailingListThread] = []

        # Extract list name from the page title or URL.
        title_el = soup.find("title")
        list_name = title_el.get_text(strip=True) if title_el else archive_url

        # Look for thread links in the archive page.
        for link in soup.find_all("a", href=True):
            link_text = link.get_text(strip=True)
            if not link_text:
                continue
            if query.lower() not in link_text.lower():
                continue
            href = link.get("href", "")
            thread_url = urljoin(archive_url, href) if href else ""

            # Try to find date and author from surrounding elements.
            parent = link.find_parent("li") or link.find_parent("tr")
            date = ""
            author = ""
            if parent:
                italic = parent.find("i") or parent.find("em")
                if italic:
                    author = italic.get_text(strip=True)
                tt = parent.find("tt") or parent.find("span", class_="date")
                if tt:
                    date = tt.get_text(strip=True)

            participants = [author] if author else []
            threads.append(
                MailingListThread(
                    fetched_via=FetchMethod.SCRAPE,
                    subject=link_text,
                    date=date,
                    participants=participants,
                    snippet="",
                    url=thread_url,
                    list_name=list_name,
                )
            )

        return MailingListSearchResult(
            fetched_via=FetchMethod.SCRAPE,
            threads=threads,
            query=query,
            list_name=list_name,
        )

    @staticmethod
    def _result_from_cache(
        data: dict[str, Any],
        method: FetchMethod,
    ) -> MailingListSearchResult:
        """Reconstruct a MailingListSearchResult from cached dict."""
        threads = [
            MailingListThread(
                fetched_via=method,
                subject=t.get("subject", ""),
                date=t.get("date", ""),
                participants=t.get("participants", []),
                snippet=t.get("snippet", ""),
                url=t.get("url", ""),
                list_name=t.get("list_name", ""),
            )
            for t in data.get("threads", [])
        ]
        return MailingListSearchResult(
            fetched_via=method,
            threads=threads,
            query=data.get("query", ""),
            list_name=data.get("list_name", ""),
        )


def _parse_archive_url(archive_url: str) -> tuple[str, str]:
    """Extract list name and domain from an Apache archive URL.

    Expected format: https://lists.apache.org/list.html?dev@spark.apache.org
    Returns: ("dev", "spark.apache.org")
    """
    if "?" in archive_url:
        query_part = archive_url.split("?", 1)[1]
        if "@" in query_part:
            list_name, domain = query_part.split("@", 1)
            return list_name, domain
    # Fallback: use reasonable defaults.
    return "dev", "spark.apache.org"
