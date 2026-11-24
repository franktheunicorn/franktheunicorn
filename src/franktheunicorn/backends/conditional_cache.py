"""Conditional-request cache for forge reads (``ETag`` / ``If-None-Match``).

GitHub doesn't count a ``304 Not Modified`` reply to a conditional request
against the primary rate limit. Polling a listing that hasn't changed is
therefore free if we send back the ``ETag`` the last reply carried — and that is
what lets the watcher (``worker/watcher.py``) ask "what changed?" every minute
instead of re-downloading every open PR every ten.

It also helps the timer-driven refreshes: most of a refresh's reads (files,
comments, reviews) come back unchanged, and each one that does is a 304.

Bounded by total body bytes, least-recently-used out first. Thread-safe: one
client is shared by the poll's refresh threads and the watcher.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

#: Total cached body bytes per client. A Spark refresh pass touches a few
#: thousand URLs, most of them small JSON lists.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

#: A single body larger than this is not worth holding (a big diff, say).
MAX_ENTRY_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class CachedReply:
    etag: str
    content: bytes
    headers: tuple[tuple[str, str], ...]


def cache_key(url: str, params: Mapping[str, object] | None, accept: str) -> str:
    """One key per (path, query, media type): the diff and the JSON of a PR differ."""
    query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    return f"{accept}|{url}?{query}"


class ConditionalCache:
    """URL → last ``200`` body and its ``ETag``, LRU-bounded by bytes."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._max_bytes = max_bytes
        self._entries: OrderedDict[str, CachedReply] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        #: 304s served from the cache, for the log line that says it is working.
        self.hits = 0

    def get(self, key: str) -> CachedReply | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, key: str, reply: CachedReply) -> None:
        size = len(reply.content)
        if size > MAX_ENTRY_BYTES or size > self._max_bytes:
            self.discard(key)
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old.content)
            self._entries[key] = reply
            self._bytes += size
            while self._bytes > self._max_bytes and self._entries:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted.content)

    def discard(self, key: str) -> None:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old.content)

    def record_hit(self) -> None:
        with self._lock:
            self.hits += 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
