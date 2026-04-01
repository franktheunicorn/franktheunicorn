"""Data types for mailing list integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from franktheunicorn.data_access.base import FetchResult


@dataclass(frozen=True)
class MailingListThread(FetchResult):
    """A single mailing list thread."""

    subject: str = ""
    date: str = ""
    participants: list[str] = field(default_factory=list)
    snippet: str = ""
    url: str = ""
    list_name: str = ""

    def to_prompt_context(self) -> str:
        """Format thread data for LLM prompt injection."""
        parts = [
            f"[{self.list_name}] {self.subject}",
            f"Date: {self.date} | Participants: {', '.join(self.participants[:5])}",
        ]
        if self.snippet:
            snip = self.snippet[:500]
            if len(self.snippet) > 500:
                snip += "... (truncated)"
            parts.append(f"Snippet: {snip}")
        if self.url:
            parts.append(f"URL: {self.url}")
        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching."""
        return {
            "subject": self.subject,
            "date": self.date,
            "participants": self.participants[:10],
            "snippet": self.snippet[:1000],
            "url": self.url,
            "list_name": self.list_name,
        }


@dataclass(frozen=True)
class MailingListSearchResult(FetchResult):
    """Result of searching a mailing list archive."""

    threads: list[MailingListThread] = field(default_factory=list)
    query: str = ""
    list_name: str = ""

    def to_prompt_context(self) -> str:
        """Format search results for LLM prompt injection."""
        if not self.threads:
            return f"No mailing list threads found for '{self.query}' on {self.list_name}."
        parts = [
            f"Mailing list search: '{self.query}' on {self.list_name}"
            f" ({len(self.threads)} threads)",
        ]
        for thread in self.threads[:5]:
            parts.append(f"  - {thread.to_prompt_context()}")
        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching."""
        return {
            "query": self.query,
            "list_name": self.list_name,
            "threads": [t.to_cache_dict() for t in self.threads[:20]],
        }
