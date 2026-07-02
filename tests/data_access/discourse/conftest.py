"""Shared fixtures for Discourse data access tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.discourse.fetcher import DiscourseFetcher

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def search_api_json() -> dict:
    return json.loads((FIXTURES_DIR / "search_api_response.json").read_text())


@pytest.fixture
def search_scrape_html() -> str:
    return (FIXTURES_DIR / "search_page.html").read_text()


@pytest.fixture
def http_client() -> httpx.Client:
    client = httpx.Client()
    yield client  # type: ignore[misc]
    client.close()


@pytest.fixture
def discourse_fetcher(http_client: httpx.Client, tmp_path: Path) -> DiscourseFetcher:
    # Inject a tmp-scoped cache — the old module-singleton cache made tests
    # read and delete files under the operator's real data/cache directory.
    cache = FileCache("discourse", cache_dir=tmp_path)
    return DiscourseFetcher(client=http_client, cache=cache)
