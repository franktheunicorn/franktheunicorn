"""Tests for the file-based JSON cache."""

from __future__ import annotations

import time
from pathlib import Path

from franktheunicorn.data_access.cache import CacheEntry, FileCache


class TestFileCache:
    def test_put_and_get(self, tmp_path: Path) -> None:
        cache = FileCache("test_source", cache_dir=tmp_path)
        cache.put("query1", "param1", data={"results": [1, 2, 3]})
        entry = cache.get("query1", "param1")
        assert entry is not None
        assert entry.data == {"results": [1, 2, 3]}
        assert entry.source == "test_source"

    def test_miss_returns_none(self, tmp_path: Path) -> None:
        cache = FileCache("test_source", cache_dir=tmp_path)
        assert cache.get("nonexistent") is None

    def test_ttl_expiry(self, tmp_path: Path) -> None:
        cache = FileCache("test_source", cache_dir=tmp_path, ttl_seconds=1)
        cache.put("query", data={"value": True})
        assert cache.get("query") is not None
        time.sleep(1.1)
        assert cache.get("query") is None

    def test_different_keys(self, tmp_path: Path) -> None:
        cache = FileCache("test_source", cache_dir=tmp_path)
        cache.put("key1", data="data1")
        cache.put("key2", data="data2")
        assert cache.get("key1") is not None
        assert cache.get("key1").data == "data1"
        assert cache.get("key2").data == "data2"

    def test_overwrite(self, tmp_path: Path) -> None:
        cache = FileCache("test_source", cache_dir=tmp_path)
        cache.put("key", data="old")
        cache.put("key", data="new")
        assert cache.get("key").data == "new"

    def test_clear(self, tmp_path: Path) -> None:
        cache = FileCache("test_source", cache_dir=tmp_path)
        cache.put("k1", data="v1")
        cache.put("k2", data="v2")
        removed = cache.clear()
        assert removed == 2
        assert cache.get("k1") is None

    def test_source_isolation(self, tmp_path: Path) -> None:
        cache_a = FileCache("source_a", cache_dir=tmp_path)
        cache_b = FileCache("source_b", cache_dir=tmp_path)
        cache_a.put("key", data="from_a")
        cache_b.put("key", data="from_b")
        assert cache_a.get("key").data == "from_a"
        assert cache_b.get("key").data == "from_b"


class TestCacheEntry:
    def test_age_human_minutes(self) -> None:
        entry = CacheEntry(data=None, cached_at=time.time() - 300, source="test", query_key="k")
        assert "5 minutes ago" in entry.age_human

    def test_age_human_hours(self) -> None:
        entry = CacheEntry(data=None, cached_at=time.time() - 7200, source="test", query_key="k")
        assert "2 hours ago" in entry.age_human

    def test_age_human_days(self) -> None:
        entry = CacheEntry(data=None, cached_at=time.time() - 172800, source="test", query_key="k")
        assert "2 days ago" in entry.age_human
