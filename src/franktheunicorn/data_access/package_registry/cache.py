"""SQLite-backed TTL cache for fetched package docs.

Keyed on ``(registry, package, version, qualified_name)``. Reuses the
existing per-process SQLite file (defaults to ``data/frank.sqlite3``)
but creates its own table so it doesn't collide with Django models.

The cache is intentionally opaque — call ``DocsCache.get()`` to fetch a
recent entry and ``DocsCache.put()`` after each network round-trip.
Stale entries are filtered on read but only removed lazily on write to
keep the read path lock-free.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from franktheunicorn.data_access.package_registry.types import PackageDocs, Registry

logger = logging.getLogger(__name__)

_TABLE = "package_registry_docs_cache"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    registry TEXT NOT NULL,
    package TEXT NOT NULL,
    version TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (registry, package, version, qualified_name)
)
"""


class DocsCache:
    """SQLite-backed cache of :class:`PackageDocs` keyed by call identity."""

    def __init__(self, db_path: str | Path, ttl_days: int = 7) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = max(0, ttl_days) * 86400
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def get(
        self,
        registry: Registry,
        package: str,
        version: str,
        qualified_name: str,
    ) -> PackageDocs | None:
        """Return a cached entry if present and not expired, else ``None``."""
        if self._ttl_seconds == 0:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload, fetched_at FROM {_TABLE} "
                "WHERE registry = ? AND package = ? AND version = ? AND qualified_name = ?",
                (registry.value, package, version, qualified_name),
            ).fetchone()
        if row is None:
            return None
        if time.time() - row["fetched_at"] > self._ttl_seconds:
            return None
        try:
            return _deserialize(row["payload"])
        except (ValueError, KeyError):
            logger.debug("Cache row failed to deserialize; ignoring", exc_info=True)
            return None

    def put(self, docs: PackageDocs) -> None:
        """Store an entry, replacing any prior one for the same key."""
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {_TABLE} "
                "(registry, package, version, qualified_name, payload, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    docs.registry.value,
                    docs.package,
                    docs.version,
                    docs.qualified_name,
                    _serialize(docs),
                    time.time(),
                ),
            )
            self._purge_expired(conn)

    def _purge_expired(self, conn: sqlite3.Connection) -> None:
        if self._ttl_seconds == 0:
            return
        cutoff = time.time() - self._ttl_seconds
        conn.execute(f"DELETE FROM {_TABLE} WHERE fetched_at < ?", (cutoff,))


def _serialize(docs: PackageDocs) -> str:
    return json.dumps(
        {
            "registry": docs.registry.value,
            "package": docs.package,
            "version": docs.version,
            "qualified_name": docs.qualified_name,
            "signature": docs.signature,
            "docstring": docs.docstring,
            "complexity_notes": docs.complexity_notes,
            "deprecated": docs.deprecated,
            "deprecation_message": docs.deprecation_message,
            "doc_url": docs.doc_url,
            "summary": docs.summary,
            "raw_warnings": list(docs.raw_warnings),
        }
    )


def _deserialize(payload: str) -> PackageDocs:
    data = json.loads(payload)
    return PackageDocs(
        registry=Registry(data["registry"]),
        package=data["package"],
        version=data["version"],
        qualified_name=data["qualified_name"],
        signature=data.get("signature", ""),
        docstring=data.get("docstring", ""),
        complexity_notes=data.get("complexity_notes", ""),
        deprecated=bool(data.get("deprecated", False)),
        deprecation_message=data.get("deprecation_message", ""),
        doc_url=data.get("doc_url", ""),
        summary=data.get("summary", ""),
        raw_warnings=list(data.get("raw_warnings", [])),
    )
