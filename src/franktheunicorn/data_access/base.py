"""Base abstractions for dual-path data fetchers."""

from __future__ import annotations

import enum
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

import httpx

from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_WEB_BASE = "https://github.com"
_GITHUB_JSON_ACCEPT = {"Accept": "application/vnd.github+json"}


class FetchMethod(enum.StrEnum):
    """Which path was used to fetch data."""

    API = "api"
    SCRAPE = "scrape"


@dataclass(frozen=True)
class FetchResult:
    """Base for all fetcher result types."""

    fetched_via: FetchMethod = field(default=FetchMethod.API)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))  # noqa: UP017


class FetchError(Exception):
    """Base error for data fetching failures."""

    def __init__(self, message: str, method: FetchMethod, status_code: int | None = None) -> None:
        super().__init__(message)
        self.method = method
        self.status_code = status_code


class RateLimitError(FetchError):
    """Raised when the API rate limit is exceeded."""


class NotFoundError(FetchError):
    """Raised when the requested resource does not exist."""


T = TypeVar("T", bound=FetchResult)


def get_login(obj: dict[str, Any]) -> str:
    """Extract ``user.login`` from a GitHub API JSON object."""
    user = obj.get("user", {})
    return user.get("login", "") if isinstance(user, dict) else str(user)


class DataFetcher(ABC, Generic[T]):  # noqa: UP046
    """Abstract dual-path fetcher: tries API first, falls back to scrape.

    Subclasses implement ``fetch_via_api`` and ``fetch_via_scrape`` with
    their own typed signatures. The ``fetch`` method provides automatic
    fallback from API to scrape on rate-limit or server errors.
    """

    def __init__(self, client: httpx.Client, rate_limiter: GitHubRateLimiter | None = None) -> None:
        self._client = client
        self._rate_limiter = rate_limiter

    def fetch(self, *args: object, **kwargs: object) -> T:
        """Try API path first; fall back to scrape on rate-limit/server errors."""
        try:
            return self.fetch_via_api(*args, **kwargs)
        except RateLimitError:
            logger.info("%s: API rate-limited, falling back to scrape", type(self).__name__)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 429, 500, 502, 503):
                logger.info(
                    "%s: API returned %s, falling back to scrape",
                    type(self).__name__,
                    exc.response.status_code,
                )
            else:
                raise
        return self.fetch_via_scrape(*args, **kwargs)

    @abstractmethod
    def fetch_via_api(self, *args: object, **kwargs: object) -> T:
        """Fetch data via the REST API."""

    @abstractmethod
    def fetch_via_scrape(self, *args: object, **kwargs: object) -> T:
        """Fetch data by scraping the HTML page."""

    # -- Shared helpers for subclasses --

    def _scrape_get(self, url: str) -> httpx.Response:
        """GET for scrape path with 404 handling."""
        response = self._client.get(url)
        if response.status_code == 404:
            raise NotFoundError(f"Not found: {url}", method=FetchMethod.SCRAPE, status_code=404)
        response.raise_for_status()
        return response

    def _api_get(self, url: str, **kwargs: object) -> httpx.Response:
        """GET with rate-limiter acquire/update and standard error handling."""
        if self._rate_limiter is not None:
            if self._rate_limiter.is_rate_limited():
                raise RateLimitError("GitHub API rate limited", method=FetchMethod.API)
            self._rate_limiter.acquire()

        response = self._client.get(url, **kwargs)  # type: ignore[arg-type]

        if self._rate_limiter is not None:
            self._rate_limiter.update_from_headers(response.headers)

        if response.status_code == 404:
            raise NotFoundError(f"Not found: {url}", method=FetchMethod.API, status_code=404)
        if response.status_code in (403, 429):
            raise RateLimitError(
                f"Rate limited ({response.status_code})",
                method=FetchMethod.API,
                status_code=response.status_code,
            )
        response.raise_for_status()
        return response

    def _api_get_json(self, url: str, **params: object) -> httpx.Response:
        """GET JSON from GitHub API (sets Accept header, passes params)."""
        return self._api_get(url, headers=_GITHUB_JSON_ACCEPT, params=params)
