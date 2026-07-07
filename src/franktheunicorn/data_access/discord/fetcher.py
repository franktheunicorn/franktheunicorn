"""Discord message search fetcher (API-only).

Discord does not have a public web interface suitable for scraping,
so this fetcher only supports the API path. The scrape path raises
``NotImplementedError``.

API path: ``GET https://discord.com/api/v10/guilds/{guild_id}/messages/search``
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.discord.types import (
    DiscordMessage,
    DiscordSearchResult,
)

logger = logging.getLogger(__name__)

_cache = FileCache("discord")

DISCORD_API_BASE = "https://discord.com/api/v10"


class DiscordFetcher:
    """Fetches Discord messages via REST API.

    Not a ``DataFetcher`` subclass because Discord is API-only;
    there is no dual-path scrape fallback.

    CAVEAT: ``/guilds/{id}/messages/search`` is an undocumented endpoint
    that Discord rejects for bot tokens (403 "Bots cannot use this
    endpoint") — with a standard bot token this source returns no data and
    logs the failure at debug. It works only with tokens that are allowed
    to search (rare). A supported approach (bot-side per-channel indexing)
    is future work; the orchestrator degrades gracefully either way.
    """

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client()

    def fetch(
        self,
        bot_token: str,
        guild_id: str,
        query: str,
        timeout_seconds: int = 30,
    ) -> DiscordSearchResult:
        """Search messages in a Discord guild.

        Returns an empty result when ``bot_token`` is empty (graceful
        degradation when Discord is not configured).
        """
        if not bot_token:
            logger.debug("Discord bot_token is empty, returning empty result")
            return DiscordSearchResult(
                fetched_via=FetchMethod.API,
                messages=[],
                query=query,
                guild_id=guild_id,
            )

        cached = _cache.get(guild_id, query)
        if cached is not None:
            logger.debug("Discord cache hit for guild=%s q=%s", guild_id, query)
            return _result_from_cache(cached.data)

        url = f"{DISCORD_API_BASE}/guilds/{guild_id}/messages/search"
        response = self._client.get(
            url,
            params={"content": query},
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()

        data: dict[str, Any] = response.json()
        result = _parse_api_response(data, query, guild_id)

        _cache.put(guild_id, query, data=result.to_cache_dict())
        return result

    def fetch_via_scrape(
        self,
        bot_token: str,
        guild_id: str,
        query: str,
        timeout_seconds: int = 30,
    ) -> DiscordSearchResult:
        """Always raises NotImplementedError.

        Discord does not support scraping.
        """
        raise NotImplementedError("Discord does not support scraping")


def _parse_api_response(
    data: dict[str, Any],
    query: str,
    guild_id: str,
) -> DiscordSearchResult:
    """Parse Discord search API JSON into a DiscordSearchResult."""
    messages: list[DiscordMessage] = []

    # Discord returns messages as list of lists (each inner list is a group
    # of context messages). The actual match carries "hit": true and is not
    # necessarily at index 0 — fall back to the first element only when no
    # hit flag is present.
    for msg_group in data.get("messages", []):
        if not msg_group:
            continue
        if isinstance(msg_group, list):
            msg = next(
                (m for m in msg_group if isinstance(m, dict) and m.get("hit")),
                msg_group[0],
            )
        else:
            msg = msg_group

        author_obj = msg.get("author", {})
        channel_id = msg.get("channel_id", "")
        message_id = msg.get("id", "")

        message_url = ""
        if channel_id and message_id:
            message_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

        messages.append(
            DiscordMessage(
                fetched_via=FetchMethod.API,
                content=msg.get("content", ""),
                author=author_obj.get("username", ""),
                channel_name=msg.get("channel_name", ""),
                timestamp=msg.get("timestamp", ""),
                message_url=message_url,
            )
        )

    return DiscordSearchResult(
        fetched_via=FetchMethod.API,
        messages=messages,
        query=query,
        guild_id=guild_id,
    )


def _result_from_cache(
    data: dict[str, Any],
) -> DiscordSearchResult:
    """Reconstruct a DiscordSearchResult from cached dict."""
    messages = [
        DiscordMessage(
            fetched_via=FetchMethod.API,
            content=m.get("content", ""),
            author=m.get("author", ""),
            channel_name=m.get("channel_name", ""),
            timestamp=m.get("timestamp", ""),
            message_url=m.get("message_url", ""),
        )
        for m in data.get("messages", [])
    ]
    return DiscordSearchResult(
        fetched_via=FetchMethod.API,
        messages=messages,
        query=data.get("query", ""),
        guild_id=data.get("guild_id", ""),
    )
