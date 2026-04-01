"""Data types for Sentry integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from franktheunicorn.data_access.base import FetchResult


@dataclass(frozen=True)
class SentryIssue:
    """A single Sentry error issue."""

    title: str = ""
    culprit: str = ""
    count: int = 0
    user_count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    short_id: str = ""


@dataclass(frozen=True)
class SentryContext(FetchResult):
    """Result of fetching Sentry issues for changed files."""

    issues: list[SentryIssue] = field(default_factory=list)
    project_slug: str = ""
    file_paths_queried: list[str] = field(default_factory=list)

    def to_prompt_context(self) -> str:
        """Format Sentry data for LLM prompt injection."""
        if not self.issues:
            return ""

        parts = [f"Sentry errors in changed files ({self.project_slug}):"]
        for issue in self.issues[:10]:
            line = f"  - {issue.title} ({issue.count} events"
            if issue.user_count:
                line += f", {issue.user_count} users"
            line += f", last seen: {issue.last_seen})"
            if issue.culprit:
                line += f"\n    Culprit: {issue.culprit}"
            parts.append(line)

        if len(self.issues) > 10:
            parts.append(f"  ... and {len(self.issues) - 10} more issues")

        return "\n".join(parts)

    def to_cache_dict(self) -> dict[str, object]:
        """Serialize for JSON caching."""
        return {
            "issues": [
                {
                    "title": i.title,
                    "culprit": i.culprit,
                    "count": i.count,
                    "user_count": i.user_count,
                    "first_seen": i.first_seen,
                    "last_seen": i.last_seen,
                    "short_id": i.short_id,
                }
                for i in self.issues
            ],
            "project_slug": self.project_slug,
            "file_paths_queried": self.file_paths_queried,
        }
