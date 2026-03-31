"""Tests for Perplexity API fetcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.perplexity.fetcher import (
    PERPLEXITY_API_URL,
    PerplexityFetcher,
)
from franktheunicorn.data_access.perplexity.types import PerplexityResult


@pytest.fixture
def perplexity_fetcher(tmp_path: Path) -> PerplexityFetcher:
    cache = FileCache("perplexity", cache_dir=tmp_path, ttl_seconds=0)
    return PerplexityFetcher(cache=cache)


class TestPerplexityFetch:
    def test_general_mode(
        self,
        httpx_mock: HTTPXMock,
        perplexity_fetcher: PerplexityFetcher,
        perplexity_general_response: dict[str, Any],
    ) -> None:
        httpx_mock.add_response(
            url=PERPLEXITY_API_URL,
            json=perplexity_general_response,
        )
        result = perplexity_fetcher.fetch("test-key", "mapInArrow", mode="general")
        assert result.mode == "general"
        assert result.query == "mapInArrow"
        assert "mapInArrow" in result.content
        assert len(result.citations) > 0

    def test_technical_mode(
        self,
        httpx_mock: HTTPXMock,
        perplexity_fetcher: PerplexityFetcher,
        perplexity_technical_response: dict[str, Any],
    ) -> None:
        httpx_mock.add_response(
            url=PERPLEXITY_API_URL,
            json=perplexity_technical_response,
        )
        result = perplexity_fetcher.fetch("test-key", "mapInArrow", mode="technical")
        assert result.mode == "technical"
        assert "PyArrow" in result.content

    def test_both_mode(
        self,
        httpx_mock: HTTPXMock,
        perplexity_fetcher: PerplexityFetcher,
        perplexity_general_response: dict[str, Any],
        perplexity_technical_response: dict[str, Any],
    ) -> None:
        httpx_mock.add_response(
            url=PERPLEXITY_API_URL,
            json=perplexity_general_response,
        )
        httpx_mock.add_response(
            url=PERPLEXITY_API_URL,
            json=perplexity_technical_response,
        )
        result = perplexity_fetcher.fetch("test-key", "mapInArrow", mode="both")
        assert result.mode == "both"
        assert "mapInArrow" in result.content
        assert len(result.citations) > 0

    def test_empty_api_key_returns_empty(
        self,
        perplexity_fetcher: PerplexityFetcher,
    ) -> None:
        result = perplexity_fetcher.fetch("", "mapInArrow")
        assert result.content == ""
        assert result.citations == []
        assert result.query == "mapInArrow"

    def test_api_error_returns_empty_content(
        self,
        httpx_mock: HTTPXMock,
        perplexity_fetcher: PerplexityFetcher,
    ) -> None:
        httpx_mock.add_response(
            url=PERPLEXITY_API_URL,
            status_code=500,
        )
        result = perplexity_fetcher.fetch("test-key", "mapInArrow", mode="general")
        assert result.content == ""

    def test_deduplicates_citations_in_both_mode(
        self,
        httpx_mock: HTTPXMock,
        perplexity_fetcher: PerplexityFetcher,
    ) -> None:
        shared_citation = "https://example.com/shared"
        httpx_mock.add_response(
            url=PERPLEXITY_API_URL,
            json={
                "choices": [{"message": {"content": "General info"}}],
                "citations": [
                    shared_citation,
                    "https://example.com/general",
                ],
            },
        )
        httpx_mock.add_response(
            url=PERPLEXITY_API_URL,
            json={
                "choices": [{"message": {"content": "Technical info"}}],
                "citations": [
                    shared_citation,
                    "https://example.com/tech",
                ],
            },
        )
        result = perplexity_fetcher.fetch("test-key", "test", mode="both")
        assert result.citations.count(shared_citation) == 1
        assert len(result.citations) == 3


class TestPerplexityResultMethods:
    def test_to_prompt_context(self) -> None:
        result = PerplexityResult(
            content="Some context about the API.",
            citations=["https://example.com/doc"],
            query="test query",
            mode="general",
        )
        ctx = result.to_prompt_context()
        assert "test query" in ctx
        assert "Some context about the API." in ctx
        assert "https://example.com/doc" in ctx

    def test_to_prompt_context_empty(self) -> None:
        result = PerplexityResult(query="test", mode="general")
        ctx = result.to_prompt_context()
        assert "test" in ctx

    def test_to_cache_dict(self) -> None:
        result = PerplexityResult(
            content="Content",
            citations=["https://example.com"],
            query="test",
            mode="technical",
        )
        d = result.to_cache_dict()
        assert d["content"] == "Content"
        assert d["mode"] == "technical"
        assert isinstance(d["citations"], list)

    def test_to_prompt_context_truncates_long_content(self) -> None:
        long_content = "x" * 3000
        result = PerplexityResult(
            content=long_content,
            query="test",
            mode="general",
        )
        ctx = result.to_prompt_context()
        assert "(truncated)" in ctx
