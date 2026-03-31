"""Shared fixtures for GitHub data access tests."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest

from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher
from franktheunicorn.data_access.github.issue_fetcher import IssueFetcher
from franktheunicorn.data_access.github.pr_fetcher import PRFetcher
from franktheunicorn.data_access.github.review_fetcher import ReviewFetcher

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# -- Raw fixture data --


@pytest.fixture
def pr_api_json() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "pr_42_api.json").read_text())


@pytest.fixture
def pr_files_api_json() -> list[dict[str, Any]]:
    return json.loads((FIXTURES_DIR / "pr_42_files_api.json").read_text())


@pytest.fixture
def pr_diff_text() -> str:
    return (FIXTURES_DIR / "pr_42_diff.diff").read_text()


@pytest.fixture
def pr_reviews_api_json() -> list[dict[str, Any]]:
    return json.loads((FIXTURES_DIR / "pr_42_reviews_api.json").read_text())


@pytest.fixture
def pr_comments_api_json() -> list[dict[str, Any]]:
    return json.loads((FIXTURES_DIR / "pr_42_comments_api.json").read_text())


@pytest.fixture
def pr_scrape_html() -> str:
    return (FIXTURES_DIR / "pr_42_scrape.html").read_text()


# -- Shared httpx client with teardown --


@pytest.fixture
def http_client() -> Generator[httpx.Client, None, None]:
    client = httpx.Client()
    yield client
    client.close()


# -- Fetcher instances --


@pytest.fixture
def diff_fetcher(http_client: httpx.Client) -> DiffFetcher:
    return DiffFetcher(client=http_client)


@pytest.fixture
def pr_fetcher(http_client: httpx.Client) -> PRFetcher:
    return PRFetcher(client=http_client)


@pytest.fixture
def review_fetcher(http_client: httpx.Client) -> ReviewFetcher:
    return ReviewFetcher(client=http_client)


# -- Issue fixtures --


@pytest.fixture
def issue_api_json() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "issue_api_response.json").read_text())


@pytest.fixture
def issue_comments_api_json() -> list[dict[str, Any]]:
    return json.loads((FIXTURES_DIR / "issue_comments_api_response.json").read_text())


@pytest.fixture
def issue_search_api_json() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "issue_search_api_response.json").read_text())


@pytest.fixture
def issue_scrape_html() -> str:
    return (FIXTURES_DIR / "issue_page.html").read_text()


@pytest.fixture
def issue_fetcher(http_client: httpx.Client, tmp_path: Path) -> IssueFetcher:
    from franktheunicorn.data_access.cache import FileCache

    cache = FileCache("github_issues", cache_dir=tmp_path, ttl_seconds=0)
    return IssueFetcher(client=http_client, cache=cache)
