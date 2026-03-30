"""
Moderation flag computation for PR routing and dashboard display.

Produces a list of string labels that describe PR characteristics useful
for triage queues (draft, bot, large, low-context, new-contributor, needs-tests).
These flags do **not** affect the numerical interest score directly.

Pure functions — no Django imports.
"""

from __future__ import annotations

from franktheunicorn.scoring.signals import LARGE_PR_THRESHOLD, is_likely_bot

# Minimum body length to avoid "low_context" flag.
_MIN_BODY_LENGTH: int = 50

# Path prefixes that indicate source code (heuristic for needs_tests).
_SOURCE_PREFIXES: tuple[str, ...] = ("src/", "lib/", "app/", "core/")

# Substring in filename that indicates a test file.
_TEST_INDICATORS: tuple[str, ...] = ("test", "spec")


def compute_moderation_flags(
    pr: dict[str, object],
    known_authors: list[str] | None = None,
) -> list[str]:
    """Compute moderation flags for a pull request.

    Parameters
    ----------
    pr:
        Dict with at least these keys (all optional — missing keys degrade
        gracefully):

        - ``author`` (str)
        - ``is_draft`` (bool)
        - ``additions`` (int)
        - ``deletions`` (int)
        - ``body`` (str)
        - ``labels`` (list[str])
        - ``changed_files`` (list[str])
    known_authors:
        Usernames of authors who have previously contributed to this project.
        Used for the ``new_contributor`` flag.

    Returns
    -------
    list[str]
        Applicable flag labels, e.g. ``["draft", "low_context"]``.
    """
    flags: list[str] = []
    author: str = str(pr.get("author", ""))

    # draft
    if pr.get("is_draft"):
        flags.append("draft")

    # bot
    if author and is_likely_bot(author):
        flags.append("bot")

    # large_pr
    additions = int(pr.get("additions", 0) or 0)
    deletions = int(pr.get("deletions", 0) or 0)
    if (additions + deletions) > LARGE_PR_THRESHOLD:
        flags.append("large_pr")

    # low_context — empty/very short body AND no labels
    body = str(pr.get("body", "") or "")
    labels: list[str] = list(pr.get("labels", []) or [])  # type: ignore[arg-type]
    if len(body.strip()) < _MIN_BODY_LENGTH and not labels:
        flags.append("low_context")

    # new_contributor
    if known_authors is not None and author:
        is_known = author.lower() in [a.lower() for a in known_authors]
        if not is_known and not is_likely_bot(author):
            flags.append("new_contributor")

    # needs_tests — source files changed but no test files present
    changed_files: list[str] = list(pr.get("changed_files", []) or [])  # type: ignore[arg-type]
    has_source = any(f.startswith(_SOURCE_PREFIXES) for f in changed_files)
    has_tests = any(any(ind in f.lower() for ind in _TEST_INDICATORS) for f in changed_files)
    if has_source and not has_tests:
        flags.append("needs_tests")

    return flags
