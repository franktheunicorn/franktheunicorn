"""Orchestrator for dependency change detection and changelog fetching.

Ties together diff parsing, changelog fetching, and Django model persistence.
Called from the worker pipeline when a PR touches dependency files.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from franktheunicorn.data_access.dependencies.changelog_fetcher import (
    PythonChangelogFetcher,
)
from franktheunicorn.data_access.dependencies.registry import parse_dependency_changes
from franktheunicorn.data_access.dependencies.types import (
    ChangelogEntry,
    Ecosystem,
    VersionTransition,
)
from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

if TYPE_CHECKING:
    from franktheunicorn.core.models import DependencyChange, PullRequest
    from franktheunicorn.data_access.github.types import PRDiff

logger = logging.getLogger(__name__)


def _get_changelog_fetcher(
    ecosystem: Ecosystem,
    client: httpx.Client,
    rate_limiter: GitHubRateLimiter | None = None,
) -> PythonChangelogFetcher | None:
    """Return the appropriate changelog fetcher for the ecosystem."""
    if ecosystem == Ecosystem.PYTHON:
        return PythonChangelogFetcher(client=client, rate_limiter=rate_limiter)
    # Future: Ecosystem.JAVA, Ecosystem.RUST
    return None


def detect_and_fetch_changelogs(
    pr: PullRequest,
    diff: PRDiff,
    client: httpx.Client,
    rate_limiter: GitHubRateLimiter | None = None,
) -> list[DependencyChange]:
    """Parse dependency changes from a PR diff, fetch changelogs, persist to DB.

    This is the main entry point called from the worker pipeline. It:
    1. Parses all dependency files in the diff for version transitions
    2. For each transition, fetches changelog/release notes
    3. Creates DependencyChange model rows

    Gracefully handles fetch failures — stores the error message but
    continues processing other dependencies.
    """
    from franktheunicorn.core.models import DependencyChange as DependencyChangeModel

    dep_diff = parse_dependency_changes(diff.files)
    if not dep_diff.transitions:
        return []

    results: list[DependencyChange] = []

    for transition in dep_diff.transitions:
        # Skip if we already have this dependency change recorded
        if DependencyChangeModel.objects.filter(
            pull_request=pr,
            package_name=transition.package_name,
            source_file=transition.source_file,
        ).exists():
            continue

        # Fetch changelog
        entry = _fetch_changelog_for_transition(transition, client, rate_limiter)

        # Persist to DB
        dep_change = DependencyChangeModel.objects.create(
            pull_request=pr,
            package_name=transition.package_name,
            ecosystem=transition.ecosystem.value,
            old_version=transition.old_version or "",
            new_version=transition.new_version or "",
            source_file=transition.source_file,
            changelog_url=entry.changelog_url if entry else "",
            changelog_text=entry.release_notes if entry else "",
            repository_url=entry.repository_url if entry else "",
            breaking_changes_detected=entry.breaking_changes_detected if entry else False,
            deprecations_detected=entry.deprecations_detected if entry else False,
            changelog_fetch_error=entry.fetch_error if entry else "",
        )
        results.append(dep_change)

    logger.info(
        "PR #%d: %d dependency changes detected, %d changelogs fetched",
        pr.number,
        len(dep_diff.transitions),
        sum(1 for r in results if not r.changelog_fetch_error),
    )
    return results


def _fetch_changelog_for_transition(
    transition: VersionTransition,
    client: httpx.Client,
    rate_limiter: GitHubRateLimiter | None,
) -> ChangelogEntry | None:
    """Fetch changelog for a single version transition, handling errors gracefully."""
    fetcher = _get_changelog_fetcher(transition.ecosystem, client, rate_limiter)
    if fetcher is None:
        logger.info(
            "No changelog fetcher for ecosystem %s (package %s)",
            transition.ecosystem,
            transition.package_name,
        )
        return None

    try:
        return fetcher.fetch(transition)
    except Exception:
        logger.exception(
            "Failed to fetch changelog for %s %s→%s",
            transition.package_name,
            transition.old_version,
            transition.new_version,
        )
        return None
