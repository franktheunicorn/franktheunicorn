"""File-based JSON cache for community context sources.

Stores cached search results as JSON files under ~/.review-agent/cache/community/.
Each source type gets its own subdirectory. Keys are SHA256 hashes of the query
parameters. TTL-based expiry with configurable defaults.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".review-agent" / "cache" / "community"
DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # 7 days


@dataclass
class CacheEntry:
    """A cached result with metadata."""

    data: Any
    cached_at: float  # Unix timestamp
    source: str
    query_key: str

    @property
    def age_seconds(self) -> float:
        return time.time() - self.cached_at

    @property
    def age_human(self) -> str:
        """Human-readable age string for prompt annotations."""
        age = self.age_seconds
        if age < 3600:
            return f"{int(age / 60)} minutes ago"
        if age < 86400:
            return f"{int(age / 3600)} hours ago"
        return f"{int(age / 86400)} days ago"


class FileCache:
    """File-based JSON cache with TTL expiry.

    Usage::

        cache = FileCache("mailing_list")
        result = cache.get("dev@spark", "mapInArrow")
        if result is None:
            data = fetch_from_source(...)
            cache.put("dev@spark", "mapInArrow", data)
    """

    def __init__(
        self,
        source_name: str,
        cache_dir: Path | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._source = source_name
        self._base_dir = (cache_dir or DEFAULT_CACHE_DIR) / source_name
        self._ttl = ttl_seconds

    def _cache_key(self, *parts: str) -> str:
        """Generate a stable cache key from query parts."""
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _cache_path(self, key: str) -> Path:
        return self._base_dir / f"{key}.json"

    def get(self, *query_parts: str) -> CacheEntry | None:
        """Return cached entry if it exists and is within TTL, else None."""
        key = self._cache_key(*query_parts)
        path = self._cache_path(key)

        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cached_at = raw.get("_cached_at", 0.0)
            age = time.time() - cached_at

            if age > self._ttl:
                logger.debug(
                    "Cache expired for %s key=%s (age=%.0fs, ttl=%ds)",
                    self._source,
                    key,
                    age,
                    self._ttl,
                )
                return None

            return CacheEntry(
                data=raw.get("data"),
                cached_at=cached_at,
                source=self._source,
                query_key=key,
            )
        except (json.JSONDecodeError, OSError):
            logger.debug("Failed to read cache file %s", path, exc_info=True)
            return None

    def put(self, *query_parts: str, data: Any) -> None:
        """Store data in the cache."""
        key = self._cache_key(*query_parts)
        path = self._cache_path(key)

        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "_cached_at": time.time(),
                "_source": self._source,
                "_query_parts": list(query_parts),
                "data": data,
            }
            path.write_text(json.dumps(payload, default=str), encoding="utf-8")
        except OSError:
            logger.debug("Failed to write cache file %s", path, exc_info=True)

    def clear(self) -> int:
        """Remove all cache files for this source. Returns count of files removed."""
        count = 0
        if self._base_dir.exists():
            for f in self._base_dir.glob("*.json"):
                f.unlink()
                count += 1
        return count
