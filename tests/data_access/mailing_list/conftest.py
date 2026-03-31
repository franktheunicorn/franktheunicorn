"""Shared fixtures for mailing list data access tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.mailing_list.fetcher import MailingListFetcher

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def search_api_json() -> dict:
    return json.loads((FIXTURES_DIR / "search_results_api.json").read_text())


@pytest.fixture
def archive_page_html() -> str:
    return (FIXTURES_DIR / "archive_page.html").read_text()


@pytest.fixture
def http_client() -> httpx.Client:
    client = httpx.Client()
    yield client  # type: ignore[misc]
    client.close()


@pytest.fixture
def tmp_cache(tmp_path: Path) -> FileCache:
    return FileCache("mailing_list", cache_dir=tmp_path)


@pytest.fixture
def ml_fetcher(http_client: httpx.Client, tmp_cache: FileCache) -> MailingListFetcher:
    return MailingListFetcher(
        client=http_client,
        delay_seconds=0.0,
        cache=tmp_cache,
    )
