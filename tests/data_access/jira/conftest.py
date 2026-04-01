"""Shared fixtures for JIRA data access tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from franktheunicorn.data_access.jira.fetcher import JiraFetcher

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def ticket_api_json() -> dict:
    return json.loads((FIXTURES_DIR / "ticket_api_response.json").read_text())


@pytest.fixture
def ticket_scrape_html() -> str:
    return (FIXTURES_DIR / "ticket_page.html").read_text()


@pytest.fixture
def http_client() -> httpx.Client:
    client = httpx.Client()
    yield client  # type: ignore[misc]
    client.close()


@pytest.fixture
def jira_fetcher(http_client: httpx.Client) -> JiraFetcher:
    return JiraFetcher(client=http_client)
