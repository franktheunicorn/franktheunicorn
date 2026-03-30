"""Dual-path data fetchers for external sources (API + scrape)."""

from franktheunicorn.data_access.base import (
    DataFetcher,
    FetchError,
    FetchMethod,
    FetchResult,
    NotFoundError,
    RateLimitError,
    get_login,
)

__all__ = [
    "DataFetcher",
    "FetchError",
    "FetchMethod",
    "FetchResult",
    "NotFoundError",
    "RateLimitError",
    "get_login",
]
