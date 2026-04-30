"""Tests for the SQLite-backed package_registry docs cache."""

from __future__ import annotations

import time

import pytest

from franktheunicorn.data_access.package_registry.cache import DocsCache
from franktheunicorn.data_access.package_registry.types import PackageDocs, Registry


@pytest.fixture
def cache(tmp_path: object) -> DocsCache:
    return DocsCache(db_path=str(tmp_path) + "/cache.sqlite", ttl_days=7)  # type: ignore[operator]


def _make_docs(qualified: str = "pandas.DataFrame.apply") -> PackageDocs:
    return PackageDocs(
        registry=Registry.PYPI,
        package="pandas",
        version="2.0.0",
        qualified_name=qualified,
        signature="DataFrame.apply(func)",
        docstring="Apply a function.",
    )


class TestDocsCache:
    def test_get_miss_returns_none(self, cache: DocsCache) -> None:
        assert (
            cache.get(Registry.PYPI, package="pandas", version="2.0.0", qualified_name="x") is None
        )

    def test_put_then_get_round_trip(self, cache: DocsCache) -> None:
        docs = _make_docs()
        cache.put(docs)
        got = cache.get(
            Registry.PYPI,
            package="pandas",
            version="2.0.0",
            qualified_name="pandas.DataFrame.apply",
        )
        assert got is not None
        assert got.signature == docs.signature
        assert got.docstring == docs.docstring
        assert got.registry is Registry.PYPI

    def test_replaces_on_duplicate_key(self, cache: DocsCache) -> None:
        cache.put(_make_docs())
        new = PackageDocs(
            registry=Registry.PYPI,
            package="pandas",
            version="2.0.0",
            qualified_name="pandas.DataFrame.apply",
            signature="changed",
        )
        cache.put(new)
        got = cache.get(
            Registry.PYPI,
            package="pandas",
            version="2.0.0",
            qualified_name="pandas.DataFrame.apply",
        )
        assert got is not None
        assert got.signature == "changed"

    def test_zero_ttl_disables_cache(self, tmp_path: object) -> None:
        cache = DocsCache(
            db_path=str(tmp_path) + "/c.sqlite",  # type: ignore[operator]
            ttl_days=0,
        )
        cache.put(_make_docs())
        assert (
            cache.get(
                Registry.PYPI,
                package="pandas",
                version="2.0.0",
                qualified_name="pandas.DataFrame.apply",
            )
            is None
        )

    def test_expired_entry_filtered_on_read(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = DocsCache(
            db_path=str(tmp_path) + "/c.sqlite",  # type: ignore[operator]
            ttl_days=1,
        )
        # Write at "current" time.
        cache.put(_make_docs())
        # Advance the clock past the TTL.
        future = time.time() + 86400 * 2
        monkeypatch.setattr(time, "time", lambda: future)
        got = cache.get(
            Registry.PYPI,
            package="pandas",
            version="2.0.0",
            qualified_name="pandas.DataFrame.apply",
        )
        assert got is None
