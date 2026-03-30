"""Moderation flag computation for PR routing (§2.2). Pure functions."""

from __future__ import annotations

from franktheunicorn.scoring.signals import LARGE_PR_THRESHOLD, _lowered, is_likely_bot

_MIN_BODY_LENGTH: int = 50
_SOURCE_PREFIXES: tuple[str, ...] = ("src/", "lib/", "app/", "core/")
_TEST_INDICATORS: tuple[str, ...] = ("test", "spec")
_DEFAULT_UNOWNED_DAYS: int = 14


def _int_field(pr: dict[str, object], key: str, default: int = 0) -> int:
    val = pr.get(key, default)
    return int(val) if isinstance(val, (int, float)) else default


def _str_list(pr: dict[str, object], key: str) -> list[str]:
    val = pr.get(key)
    if isinstance(val, list):
        return [str(x) for x in val]
    return []


def compute_moderation_flags(
    pr: dict[str, object],
    operator_username: str,
    known_authors: list[str] | None = None,
) -> list[str]:
    """Return flag labels for routing: is_operator_pr, draft, bot, large_pr,
    low_context, new_contributor, needs_tests, likely_unowned."""
    flags: list[str] = []
    author = str(pr.get("author", ""))

    if author and author.lower() == operator_username.lower():
        flags.append("is_operator_pr")

    if pr.get("is_draft"):
        flags.append("draft")

    if author and is_likely_bot(author):
        flags.append("bot")

    if (_int_field(pr, "additions") + _int_field(pr, "deletions")) > LARGE_PR_THRESHOLD:
        flags.append("large_pr")

    body = str(pr.get("body", "") or "")
    labels = _str_list(pr, "labels")
    if len(body.strip()) < _MIN_BODY_LENGTH and not labels:
        flags.append("low_context")

    if (
        known_authors is not None
        and author
        and author.lower() not in _lowered(known_authors)
        and not is_likely_bot(author)
    ):
        flags.append("new_contributor")

    changed = _str_list(pr, "changed_files")
    has_source = any(f.startswith(_SOURCE_PREFIXES) for f in changed)
    has_tests = any(any(ind in f.lower() for ind in _TEST_INDICATORS) for f in changed)
    if has_source and not has_tests:
        flags.append("needs_tests")

    pr_age_days = _int_field(pr, "pr_age_days", -1)
    reviewers = _str_list(pr, "requested_reviewers")
    if pr_age_days > _DEFAULT_UNOWNED_DAYS and not reviewers and "draft" not in flags:
        flags.append("likely_unowned")

    return flags
