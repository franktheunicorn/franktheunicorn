"""Base abstractions for dual-path data fetchers."""

from __future__ import annotations

import enum
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Generic, TypeVar

import httpx

logger = logging.getLogger(__name__)


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


class ParseError(FetchError):
    """Raised when response parsing fails."""


T = TypeVar("T", bound=FetchResult)


class DataFetcher(ABC, Generic[T]):  # noqa: UP046
    """Abstract dual-path fetcher: tries API first, falls back to scrape.

    Subclasses implement ``fetch_via_api`` and ``fetch_via_scrape`` with
    their own typed signatures. The ``fetch`` method provides automatic
    fallback from API to scrape on rate-limit or server errors.
    """

    def __init__(self, client: httpx.Client, rate_limiter: object | None = None) -> None:
        self._client = client
        self._rate_limiter = rate_limiter

    def fetch(self, *args: object, **kwargs: object) -> T:
        """Try API path first; fall back to scrape on rate-limit/server errors."""
        try:
            return self.fetch_via_api(*args, **kwargs)  # type: ignore[arg-type]
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
        return self.fetch_via_scrape(*args, **kwargs)  # type: ignore[arg-type]

    @abstractmethod
    def fetch_via_api(self, *args: object, **kwargs: object) -> T:
        """Fetch data via the REST API."""

    @abstractmethod
    def fetch_via_scrape(self, *args: object, **kwargs: object) -> T:
        """Fetch data by scraping the HTML page."""
