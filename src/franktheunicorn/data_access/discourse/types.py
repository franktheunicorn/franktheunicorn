"""Data types for Discourse forum integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from franktheunicorn.data_access.base import FetchResult


@dataclass(frozen=True)
class DiscoursePost(FetchResult):
    """A single Discourse forum post."""

    title: str = ""
    url: str = ""
    excerpt: str = ""
    date: str = ""
    author: str = ""
    category: str = ""

    def to_prompt_context(self) -> str:
        """Format post data for LLM prompt injection."""
        parts = [f"[{self.date}] {self.title}"]
        if self.author:
            parts[0] += f" (by {self.author})"
        if self.category:
            parts.append(f"  Category: {self.category}")
        if self.excerpt:
            excerpt = self.excerpt[:500]
            if len(self.excerpt) > 500:
                excerpt += "... (truncated)"
            parts.append(f"  {excerpt}")
        if self.url:
            parts.append(f"  {self.url}")
        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching."""
        return {
            "title": self.title,
            "url": self.url,
            "excerpt": self.excerpt[:1000],
            "date": self.date,
            "author": self.author,
            "category": self.category,
        }


@dataclass(frozen=True)
class DiscourseSearchResult(FetchResult):
    """Result of searching a Discourse forum."""

    posts: list[DiscoursePost] = field(default_factory=list)
    query: str = ""
    base_url: str = ""

    def to_prompt_context(self) -> str:
        """Format search results for LLM prompt injection."""
        if not self.posts:
            return f"Discourse search for '{self.query}': no results"
        parts = [f"Discourse search for '{self.query}' ({len(self.posts)} results):"]
        for post in self.posts[:10]:
            parts.append(post.to_prompt_context())
        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching."""
        return {
            "query": self.query,
            "base_url": self.base_url,
            "posts": [p.to_cache_dict() for p in self.posts],
        }
