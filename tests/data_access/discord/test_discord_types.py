"""Tests for Discord data types (DiscordMessage, DiscordSearchResult)."""

from __future__ import annotations

from franktheunicorn.data_access.discord.types import DiscordMessage, DiscordSearchResult


class TestDiscordMessageToPromptContext:
    def test_basic_message(self) -> None:
        msg = DiscordMessage(
            content="Hello world",
            author="alice",
            channel_name="general",
            timestamp="2026-03-20T10:00:00Z",
            message_url="https://discord.com/channels/1/2/3",
        )
        result = msg.to_prompt_context()
        assert "[2026-03-20T10:00:00Z] #general @alice" in result
        assert "  Hello world" in result
        assert "  https://discord.com/channels/1/2/3" in result

    def test_no_author(self) -> None:
        msg = DiscordMessage(
            content="Hello",
            author="",
            channel_name="general",
            timestamp="2026-03-20T10:00:00Z",
        )
        result = msg.to_prompt_context()
        assert "@" not in result
        assert "[2026-03-20T10:00:00Z] #general" in result

    def test_no_content(self) -> None:
        msg = DiscordMessage(
            content="",
            author="alice",
            channel_name="general",
            timestamp="2026-03-20T10:00:00Z",
        )
        result = msg.to_prompt_context()
        lines = result.strip().split("\n")
        # Should only have the header line, no content line
        assert len(lines) == 1

    def test_content_truncated_at_500(self) -> None:
        long_content = "x" * 600
        msg = DiscordMessage(
            content=long_content,
            author="alice",
            channel_name="general",
            timestamp="2026-03-20T10:00:00Z",
        )
        result = msg.to_prompt_context()
        assert "... (truncated)" in result
        # The content portion should have 500 chars + truncation marker
        assert "x" * 500 in result
        assert "x" * 501 not in result

    def test_content_exactly_500_not_truncated(self) -> None:
        content = "y" * 500
        msg = DiscordMessage(
            content=content,
            author="alice",
            channel_name="general",
            timestamp="2026-03-20T10:00:00Z",
        )
        result = msg.to_prompt_context()
        assert "truncated" not in result

    def test_no_message_url(self) -> None:
        msg = DiscordMessage(
            content="Hello",
            author="alice",
            channel_name="general",
            timestamp="2026-03-20T10:00:00Z",
            message_url="",
        )
        result = msg.to_prompt_context()
        lines = result.strip().split("\n")
        assert len(lines) == 2  # header + content, no URL line


class TestDiscordMessageToCacheDict:
    def test_basic_serialization(self) -> None:
        msg = DiscordMessage(
            content="Hello world",
            author="alice",
            channel_name="general",
            timestamp="2026-03-20T10:00:00Z",
            message_url="https://discord.com/channels/1/2/3",
        )
        d = msg.to_cache_dict()
        assert d["content"] == "Hello world"
        assert d["author"] == "alice"
        assert d["channel_name"] == "general"
        assert d["timestamp"] == "2026-03-20T10:00:00Z"
        assert d["message_url"] == "https://discord.com/channels/1/2/3"

    def test_content_truncated_at_2000(self) -> None:
        long_content = "z" * 3000
        msg = DiscordMessage(content=long_content, channel_name="ch", timestamp="t")
        d = msg.to_cache_dict()
        assert len(d["content"]) == 2000


class TestDiscordSearchResultToPromptContext:
    def test_no_results(self) -> None:
        result = DiscordSearchResult(query="spark error", guild_id="123")
        text = result.to_prompt_context()
        assert text == "Discord search for 'spark error': no results"

    def test_with_messages(self) -> None:
        messages = [
            DiscordMessage(
                content=f"Message {i}",
                author="user",
                channel_name="dev",
                timestamp=f"2026-03-{20 + i}T10:00:00Z",
            )
            for i in range(3)
        ]
        result = DiscordSearchResult(query="test", guild_id="123", messages=messages)
        text = result.to_prompt_context()
        assert "Discord search for 'test' (3 results):" in text
        assert "Message 0" in text
        assert "Message 2" in text

    def test_limits_to_ten_messages(self) -> None:
        messages = [
            DiscordMessage(
                content=f"Msg {i}",
                author="user",
                channel_name="dev",
                timestamp="2026-03-20T10:00:00Z",
            )
            for i in range(15)
        ]
        result = DiscordSearchResult(query="test", guild_id="123", messages=messages)
        text = result.to_prompt_context()
        assert "15 results" in text
        assert "Msg 9" in text
        assert "Msg 10" not in text


class TestDiscordSearchResultToCacheDict:
    def test_basic_serialization(self) -> None:
        msg = DiscordMessage(
            content="Hello",
            author="alice",
            channel_name="general",
            timestamp="2026-03-20T10:00:00Z",
            message_url="https://discord.com/channels/1/2/3",
        )
        result = DiscordSearchResult(query="test", guild_id="456", messages=[msg])
        d = result.to_cache_dict()
        assert d["query"] == "test"
        assert d["guild_id"] == "456"
        assert len(d["messages"]) == 1
        assert d["messages"][0]["content"] == "Hello"
        assert d["messages"][0]["author"] == "alice"

    def test_empty_messages(self) -> None:
        result = DiscordSearchResult(query="q", guild_id="1")
        d = result.to_cache_dict()
        assert d["messages"] == []
