"""Shared fixtures for GitHub data access tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher
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


# -- Fetcher instances (bound to a fresh httpx.Client) --


@pytest.fixture
def diff_fetcher() -> DiffFetcher:
    return DiffFetcher(client=httpx.Client())


@pytest.fixture
def pr_fetcher() -> PRFetcher:
    return PRFetcher(client=httpx.Client())


@pytest.fixture
def review_fetcher() -> ReviewFetcher:
    return ReviewFetcher(client=httpx.Client())
