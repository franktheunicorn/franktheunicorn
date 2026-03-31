"""Data types for JIRA integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from franktheunicorn.data_access.base import FetchResult


@dataclass(frozen=True)
class JiraComment:
    """A single JIRA issue comment."""

    author: str
    body: str
    created: str


@dataclass(frozen=True)
class JiraTicketResult(FetchResult):
    """Result of fetching a JIRA ticket."""

    ticket_id: str = ""
    summary: str = ""
    description: str = ""
    status: str = ""
    assignee: str = ""
    priority: str = ""
    issue_type: str = ""
    project: str = ""
    recent_comments: list[JiraComment] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        """Format ticket data for LLM prompt injection."""
        parts = [
            f"JIRA {self.ticket_id}: {self.summary}",
            f"Status: {self.status} | Priority: {self.priority} | Assignee: {self.assignee}",
        ]
        if self.description:
            desc = self.description[:1000]
            if len(self.description) > 1000:
                desc += "... (truncated)"
            parts.append(f"Description: {desc}")
        if self.recent_comments:
            parts.append("Recent comments:")
            for comment in self.recent_comments[:5]:
                body = comment.body[:300]
                if len(comment.body) > 300:
                    body += "..."
                parts.append(f"  [{comment.author}] {body}")
        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching on PullRequest model."""
        return {
            "ticket_id": self.ticket_id,
            "summary": self.summary,
            "description": self.description[:2000],
            "status": self.status,
            "assignee": self.assignee,
            "priority": self.priority,
            "issue_type": self.issue_type,
            "project": self.project,
            "recent_comments": [
                {"author": c.author, "body": c.body[:500], "created": c.created}
                for c in self.recent_comments[:5]
            ],
        }
