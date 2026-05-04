"""Shared test helpers for curator tests."""

from __future__ import annotations

from franktheunicorn.curator.classifier import ClassifiedComment
from franktheunicorn.curator.scraper import RawComment

_RAW_DEFAULTS = {
    "author": "alice",
    "diff_context": "@@ -1 +1 @@\n-old\n+new",
    "file_path": "src/main.py",
    "pr_number": 42,
    "pr_title": "Fix bug",
    "created_at": "2026-03-20T10:00:00Z",
    "url": "https://github.com/org/repo/pull/42#r1",
}


def make_raw_comment(body: str = "Fix this bug", **kwargs: object) -> RawComment:
    """Create a RawComment with sensible defaults for testing."""
    fields = {**_RAW_DEFAULTS, "body": body, **kwargs}
    return RawComment(**fields)


def make_classified_comment(
    body: str = "Fix this bug",
    category: str = "correctness",
    tone_flagged: bool = False,
    tone_flags: list[str] | None = None,
) -> ClassifiedComment:
    """Create a ClassifiedComment with sensible defaults for testing."""
    return ClassifiedComment(
        raw=make_raw_comment(body=body),
        category=category,
        tone_flagged=tone_flagged,
        tone_flags=tone_flags or [],
    )
