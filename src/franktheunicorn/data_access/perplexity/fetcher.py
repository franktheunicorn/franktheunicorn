"""Perplexity AI search fetcher.

API-only fetcher (no scrape path) that queries Perplexity's sonar model
for general and technical context about code changes.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.perplexity.types import PerplexityResult

logger = logging.getLogger(__name__)

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar"

_GENERAL_SYSTEM_PROMPT = "Search for relevant discussions about: {query}"
_TECHNICAL_SYSTEM_PROMPT = "Find API docs, deprecation notices, or known issues for: {query}"


class PerplexityFetcher:
    """Fetches context from Perplexity AI search API.

    This is NOT a DataFetcher subclass -- Perplexity is API-only,
    there is no scrape fallback.
    """

    def __init__(self, cache: FileCache | None = None) -> None:
        self._cache = cache or FileCache(source_name="perplexity")

    def fetch(
        self,
        api_key: str,
        query: str,
        mode: str = "both",
        timeout_seconds: int = 30,
    ) -> PerplexityResult:
        """Query Perplexity for context.

        Args:
            api_key: Perplexity API key. If empty, returns empty result.
            query: The search query.
            mode: One of "general", "technical", or "both".
            timeout_seconds: HTTP request timeout.

        Returns:
            PerplexityResult with content and citations.
        """
        if not api_key:
            logger.debug("No Perplexity API key configured, returning empty result")
            return PerplexityResult(query=query, mode=mode)

        cached = self._cache.get(query, mode)
        if cached is not None:
            return self._from_cache_dict(cached.data, query, mode)

        had_errors = False
        if mode == "both":
            general = self._query_api(api_key, query, "general", timeout_seconds)
            technical = self._query_api(api_key, query, "technical", timeout_seconds)
            had_errors = general is None or technical is None
            content = ""
            citations: list[str] = []
            if general:
                content += general.get("content", "")
                citations.extend(general.get("citations", []))
            if technical:
                if content:
                    content += "\n\n---\n\n"
                content += technical.get("content", "")
                citations.extend(technical.get("citations", []))
            # Deduplicate citations while preserving order.
            seen: set[str] = set()
            deduped: list[str] = []
            for c in citations:
                if c not in seen:
                    seen.add(c)
                    deduped.append(c)
            citations = deduped
        else:
            raw = self._query_api(api_key, query, mode, timeout_seconds)
            had_errors = raw is None
            content = raw.get("content", "") if raw else ""
            citations = raw.get("citations", []) if raw else []

        result = PerplexityResult(
            content=content,
            citations=citations,
            query=query,
            mode=mode,
        )
        # Don't cache emptiness caused by API failures — a transient outage
        # would otherwise blank this query's context for the whole TTL.
        if content or not had_errors:
            self._cache.put(query, mode, data=result.to_cache_dict())
        return result

    def _query_api(
        self,
        api_key: str,
        query: str,
        mode: str,
        timeout_seconds: int,
    ) -> dict[str, Any] | None:
        """Make a single Perplexity API call."""
        if mode == "technical":
            system_prompt = _TECHNICAL_SYSTEM_PROMPT.format(query=query)
        else:
            system_prompt = _GENERAL_SYSTEM_PROMPT.format(query=query)

        payload = {
            "model": PERPLEXITY_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
        }

        try:
            response = httpx.post(
                PERPLEXITY_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

            content = ""
            choices = data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                content = message.get("content", "")

            citations = data.get("citations", [])

            return {"content": content, "citations": citations}
        except (httpx.HTTPError, KeyError, IndexError):
            logger.debug(
                "Perplexity API call failed for query=%s mode=%s",
                query,
                mode,
                exc_info=True,
            )
            return None

    @staticmethod
    def _from_cache_dict(data: dict[str, Any], query: str, mode: str) -> PerplexityResult:
        """Reconstruct a PerplexityResult from cached dict."""
        return PerplexityResult(
            content=data.get("content", ""),
            citations=data.get("citations", []),
            query=query,
            mode=mode,
        )
