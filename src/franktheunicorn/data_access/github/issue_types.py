"""Typed dataclasses for GitHub issue data."""

from __future__ import annotations

from dataclasses import dataclass, field

from franktheunicorn.data_access.base import FetchResult


@dataclass(frozen=True)
class IssueComment:
    """A single comment on a GitHub issue."""

    author: str
    body: str


@dataclass(frozen=True)
class GitHubIssueResult(FetchResult):
    """Result of fetching a GitHub issue."""

    number: int = 0
    title: str = ""
    body: str = ""
    state: str = "open"
    labels: list[str] = field(default_factory=list)
    author: str = ""
    url: str = ""
    comments: list[IssueComment] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        """Format issue data for LLM prompt injection."""
        parts = [
            f"GitHub Issue #{self.number}: {self.title}",
            f"State: {self.state} | Author: {self.author}",
        ]
        if self.labels:
            parts.append(f"Labels: {', '.join(self.labels)}")
        if self.body:
            body = self.body[:1000]
            if len(self.body) > 1000:
                body += "... (truncated)"
            parts.append(f"Body: {body}")
        if self.comments:
            # Both fetch paths return comments oldest-first (API default,
            # page order) — label honestly rather than claiming recency.
            parts.append("First comments:")
            for comment in self.comments[:5]:
                body = comment.body[:300]
                if len(comment.body) > 300:
                    body += "..."
                parts.append(f"  [{comment.author}] {body}")
        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching."""
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body[:2000],
            "state": self.state,
            "labels": self.labels,
            "author": self.author,
            "url": self.url,
            "comments": [{"author": c.author, "body": c.body[:500]} for c in self.comments[:5]],
        }
