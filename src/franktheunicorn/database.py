"""Database engine and session management.

Local-first: SQLite only.  The database file lives in the mounted data/ volume.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from franktheunicorn.config import get_settings
from franktheunicorn.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    """Enable WAL mode and foreign keys for SQLite connections."""

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn: Any, _: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine() -> Engine:
    """Return (and lazily create) the global SQLAlchemy engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )
        if "sqlite" in settings.database_url:
            _configure_sqlite(_engine)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return (and lazily create) the global session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)
    return _SessionLocal


def create_all_tables() -> None:
    """Create all tables (idempotent - use Alembic for migrations in production)."""
    Base.metadata.create_all(bind=get_engine())


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Provide a transactional database session as a context manager."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Discard the cached engine (useful in tests that swap databases)."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
