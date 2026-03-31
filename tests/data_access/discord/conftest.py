"""Shared fixtures for Discord data access tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from franktheunicorn.data_access.discord import fetcher as discord_fetcher_mod
from franktheunicorn.data_access.discord.fetcher import DiscordFetcher

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def search_api_json() -> dict:
    return json.loads((FIXTURES_DIR / "search_api_response.json").read_text())


@pytest.fixture
def http_client() -> httpx.Client:
    client = httpx.Client()
    yield client  # type: ignore[misc]
    client.close()


@pytest.fixture(autouse=True)
def _clear_discord_cache() -> None:
    """Clear the module-level cache before each test."""
    discord_fetcher_mod._cache.clear()


@pytest.fixture
def discord_fetcher(http_client: httpx.Client) -> DiscordFetcher:
    return DiscordFetcher(client=http_client)
