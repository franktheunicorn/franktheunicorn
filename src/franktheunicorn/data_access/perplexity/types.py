"""Data types for Perplexity search integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from franktheunicorn.data_access.base import FetchResult


@dataclass(frozen=True)
class PerplexityResult(FetchResult):
    """Result of a Perplexity search query."""

    content: str = ""
    citations: list[str] = field(default_factory=list)
    query: str = ""
    mode: str = "general"  # "general", "technical", or "both"

    def to_prompt_context(self) -> str:
        """Format Perplexity result for LLM prompt injection."""
        parts = [f"Perplexity search ({self.mode}): {self.query}"]
        if self.content:
            content = self.content[:2000]
            if len(self.content) > 2000:
                content += "... (truncated)"
            parts.append(content)
        if self.citations:
            parts.append("Sources:")
            for citation in self.citations[:10]:
                parts.append(f"  - {citation}")
        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching."""
        return {
            "content": self.content[:5000],
            "citations": self.citations[:10],
            "query": self.query,
            "mode": self.mode,
        }
