"""Tests for Discord REST API fetch path."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.discord.fetcher import DiscordFetcher

GUILD_ID = "800001"
BOT_TOKEN = "test-bot-token-abc123"


class TestDiscordAPIFetch:
    def test_fetches_messages(
        self,
        httpx_mock: HTTPXMock,
        discord_fetcher: DiscordFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(
            url=(
                f"https://discord.com/api/v10/guilds/{GUILD_ID}/messages/search?content=mapInArrow"
            ),
            json=search_api_json,
        )
        result = discord_fetcher.fetch(BOT_TOKEN, GUILD_ID, "mapInArrow")
        assert result.fetched_via == FetchMethod.API
        assert result.query == "mapInArrow"
        assert result.guild_id == GUILD_ID
        assert len(result.messages) == 2

    def test_parses_message_fields(
        self,
        httpx_mock: HTTPXMock,
        discord_fetcher: DiscordFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(
            url=(
                f"https://discord.com/api/v10/guilds/{GUILD_ID}/messages/search?content=mapInArrow"
            ),
            json=search_api_json,
        )
        result = discord_fetcher.fetch(BOT_TOKEN, GUILD_ID, "mapInArrow")
        msg = result.messages[0]
        assert msg.author == "holdenk"
        assert "mapInArrow" in msg.content
        assert msg.timestamp == "2025-11-10T09:15:00Z"
        assert GUILD_ID in msg.message_url

    def test_constructs_message_urls(
        self,
        httpx_mock: HTTPXMock,
        discord_fetcher: DiscordFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(
            url=(
                f"https://discord.com/api/v10/guilds/{GUILD_ID}/messages/search?content=mapInArrow"
            ),
            json=search_api_json,
        )
        result = discord_fetcher.fetch(BOT_TOKEN, GUILD_ID, "mapInArrow")
        expected_url = f"https://discord.com/channels/{GUILD_ID}/900001/1100001"
        assert result.messages[0].message_url == expected_url

    def test_sends_auth_header(
        self,
        httpx_mock: HTTPXMock,
        discord_fetcher: DiscordFetcher,
        search_api_json: dict,
    ) -> None:
        httpx_mock.add_response(
            url=(f"https://discord.com/api/v10/guilds/{GUILD_ID}/messages/search?content=test"),
            json=search_api_json,
        )
        discord_fetcher.fetch(BOT_TOKEN, GUILD_ID, "test")
        request = httpx_mock.get_request()
        assert request is not None
        assert request.headers["Authorization"] == f"Bot {BOT_TOKEN}"


class TestDiscordEmptyToken:
    def test_empty_token_returns_empty_result(self, discord_fetcher: DiscordFetcher) -> None:
        result = discord_fetcher.fetch("", GUILD_ID, "mapInArrow")
        assert result.messages == []
        assert result.query == "mapInArrow"
        assert result.guild_id == GUILD_ID

    def test_empty_result_prompt_context(self, discord_fetcher: DiscordFetcher) -> None:
        result = discord_fetcher.fetch("", GUILD_ID, "test")
        ctx = result.to_prompt_context()
        assert "no results" in ctx


class TestDiscordScrapeNotSupported:
    def test_scrape_raises_not_implemented(self, discord_fetcher: DiscordFetcher) -> None:
        with pytest.raises(NotImplementedError, match="scraping"):
            discord_fetcher.fetch_via_scrape(BOT_TOKEN, GUILD_ID, "test")
