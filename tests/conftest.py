"""Shared test fixtures and configuration."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from franktheunicorn.config import OperatorConfig, ProjectConfig, Settings, override_settings
from franktheunicorn.database import reset_engine
from franktheunicorn.models import Base


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path):
    """Use an in-memory SQLite DB for each test and reset engine."""
    db_path = tmp_path / "test.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        github_token="fake-token",
        operator_config_path=str(tmp_path / "operator.yaml"),
        projects_config_dir=str(tmp_path / "projects"),
    )
    override_settings(settings)
    reset_engine()
    yield settings
    reset_engine()
    override_settings(Settings.__new__(Settings))  # type: ignore[call-arg]


@pytest.fixture()
def db_session(isolated_settings) -> Session:
    """Provide a test DB session with all tables created."""
    engine = create_engine(
        isolated_settings.database_url,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    engine.dispose()


@pytest.fixture()
def operator() -> OperatorConfig:
    return OperatorConfig(
        github_login="franktheunicorn",
        email="frank@example.com",
        trusted_collaborators=["alice", "bob"],
        stale_pr_days=30,
    )


@pytest.fixture()
def project() -> ProjectConfig:
    return ProjectConfig(
        slug="myproject",
        repo="example/myproject",
        watched_paths=["src/core/**", "src/api/**"],
        frequent_contributors=["carol"],
        asf_project=False,
        poll_interval_seconds=300,
        max_prs_per_poll=50,
    )
