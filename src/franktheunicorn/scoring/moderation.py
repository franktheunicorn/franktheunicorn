"""Moderation flag computation for PR routing. Pure functions."""

from __future__ import annotations

from franktheunicorn.scoring.signals import LARGE_PR_THRESHOLD, _lowered, is_likely_bot

_MIN_BODY_LENGTH: int = 50
_SOURCE_PREFIXES: tuple[str, ...] = ("src/", "lib/", "app/", "core/")
_TEST_INDICATORS: tuple[str, ...] = ("test", "spec")


def compute_moderation_flags(
    pr: dict[str, object],
    known_authors: list[str] | None = None,
) -> list[str]:
    """Return flag labels for a PR: draft, bot, large_pr, low_context,
    new_contributor, needs_tests. All keys optional — missing keys degrade gracefully."""
    flags: list[str] = []
    author = str(pr.get("author", ""))

    if pr.get("is_draft"):
        flags.append("draft")

    if author and is_likely_bot(author):
        flags.append("bot")

    additions = int(pr.get("additions", 0) or 0)
    deletions = int(pr.get("deletions", 0) or 0)
    if (additions + deletions) > LARGE_PR_THRESHOLD:
        flags.append("large_pr")

    body = str(pr.get("body", "") or "")
    labels: list[object] = list(pr.get("labels", []) or [])
    if len(body.strip()) < _MIN_BODY_LENGTH and not labels:
        flags.append("low_context")

    if (
        known_authors is not None
        and author
        and author.lower() not in _lowered(known_authors)
        and not is_likely_bot(author)
    ):
        flags.append("new_contributor")

    changed: list[str] = list(pr.get("changed_files", []) or [])  # type: ignore[arg-type]
    has_source = any(f.startswith(_SOURCE_PREFIXES) for f in changed)
    has_tests = any(any(ind in f.lower() for ind in _TEST_INDICATORS) for f in changed)
    if has_source and not has_tests:
        flags.append("needs_tests")

    return flags
