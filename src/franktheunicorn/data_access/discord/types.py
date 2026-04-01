"""Data types for Discord integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from franktheunicorn.data_access.base import FetchResult


@dataclass(frozen=True)
class DiscordMessage(FetchResult):
    """A single Discord message."""

    content: str = ""
    author: str = ""
    channel_name: str = ""
    timestamp: str = ""
    message_url: str = ""

    def to_prompt_context(self) -> str:
        """Format message data for LLM prompt injection."""
        parts = [f"[{self.timestamp}] #{self.channel_name}"]
        if self.author:
            parts[0] += f" @{self.author}"
        if self.content:
            content = self.content[:500]
            if len(self.content) > 500:
                content += "... (truncated)"
            parts.append(f"  {content}")
        if self.message_url:
            parts.append(f"  {self.message_url}")
        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching."""
        return {
            "content": self.content[:2000],
            "author": self.author,
            "channel_name": self.channel_name,
            "timestamp": self.timestamp,
            "message_url": self.message_url,
        }


@dataclass(frozen=True)
class DiscordSearchResult(FetchResult):
    """Result of searching Discord messages in a guild."""

    messages: list[DiscordMessage] = field(default_factory=list)
    query: str = ""
    guild_id: str = ""

    def to_prompt_context(self) -> str:
        """Format search results for LLM prompt injection."""
        if not self.messages:
            return f"Discord search for '{self.query}': no results"
        parts = [f"Discord search for '{self.query}' ({len(self.messages)} results):"]
        for msg in self.messages[:10]:
            parts.append(msg.to_prompt_context())
        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching."""
        return {
            "query": self.query,
            "guild_id": self.guild_id,
            "messages": [m.to_cache_dict() for m in self.messages],
        }
