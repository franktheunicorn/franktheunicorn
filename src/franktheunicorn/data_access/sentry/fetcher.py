"""Sentry issue fetcher for changed files.

API-only fetcher (no scrape path) that queries Sentry for errors
related to files changed in a pull request.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.sentry.types import SentryContext, SentryIssue

logger = logging.getLogger(__name__)

SENTRY_API_BASE = "https://sentry.io/api/0"


class SentryFetcher:
    """Fetches Sentry issues related to changed files.

    This is NOT a DataFetcher subclass -- Sentry is API-only,
    there is no scrape fallback.
    """

    def __init__(self, cache: FileCache | None = None) -> None:
        self._cache = cache or FileCache(source_name="sentry")

    def fetch_issues_for_files(
        self,
        auth_token: str,
        org_slug: str,
        project_slug: str,
        file_paths: list[str],
        timeout_seconds: int = 30,
    ) -> SentryContext:
        """Fetch Sentry issues for the given file paths.

        Args:
            auth_token: Sentry auth token. If empty, returns empty context.
            org_slug: Sentry organization slug.
            project_slug: Sentry project slug.
            file_paths: List of file paths changed in the PR.
            timeout_seconds: HTTP request timeout.

        Returns:
            SentryContext with deduplicated issues across all queried files.
        """
        if not auth_token:
            logger.debug("No Sentry auth token configured, returning empty context")
            return SentryContext(
                project_slug=project_slug,
                file_paths_queried=file_paths,
            )

        cache_key_paths = ",".join(sorted(file_paths))
        cached = self._cache.get(org_slug, project_slug, cache_key_paths)
        if cached is not None:
            return self._from_cache_dict(cached.data)

        seen_ids: set[str] = set()
        all_issues: list[SentryIssue] = []
        had_errors = False

        for path in file_paths:
            issues = self._fetch_for_path(auth_token, org_slug, project_slug, path, timeout_seconds)
            if issues is None:
                had_errors = True
                continue
            for issue in issues:
                if issue.short_id and issue.short_id not in seen_ids:
                    seen_ids.add(issue.short_id)
                    all_issues.append(issue)
                elif not issue.short_id:
                    all_issues.append(issue)

        result = SentryContext(
            issues=all_issues,
            project_slug=project_slug,
            file_paths_queried=file_paths,
        )

        # Don't cache an empty result produced by request failures — a
        # transient outage would otherwise suppress Sentry context for the
        # whole cache TTL.
        if all_issues or not had_errors:
            self._cache.put(
                org_slug,
                project_slug,
                cache_key_paths,
                data=result.to_cache_dict(),
            )
        return result

    def _fetch_for_path(
        self,
        auth_token: str,
        org_slug: str,
        project_slug: str,
        file_path: str,
        timeout_seconds: int,
    ) -> list[SentryIssue] | None:
        """Fetch Sentry issues for a single file path.

        Returns ``None`` on request failure (as opposed to an empty list for
        "no issues") so the caller can avoid caching outages as results.
        """
        url = f"{SENTRY_API_BASE}/projects/{org_slug}/{project_slug}/issues/"
        try:
            response = httpx.get(
                url,
                # Sentry's issue-search grammar has no "file:" key (invalid
                # keys 400) — the stack filename property is the right one.
                # statsPeriod matches the 24h window the prompt label claims.
                params={"query": f'stack.filename:"{file_path}"', "statsPeriod": "24h"},
                headers={"Authorization": f"Bearer {auth_token}"},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()

            return [self._parse_issue(item) for item in data]
        except (httpx.HTTPError, KeyError):
            logger.debug(
                "Sentry API call failed for path=%s",
                file_path,
                exc_info=True,
            )
            return None

    @staticmethod
    def _parse_issue(data: dict[str, Any]) -> SentryIssue:
        """Parse a single Sentry issue from API JSON."""
        return SentryIssue(
            title=data.get("title", ""),
            culprit=data.get("culprit", ""),
            count=int(data.get("count", 0)),
            user_count=int(data.get("userCount", 0)),
            first_seen=data.get("firstSeen", ""),
            last_seen=data.get("lastSeen", ""),
            short_id=data.get("shortId", ""),
        )

    @staticmethod
    def _from_cache_dict(data: dict[str, Any]) -> SentryContext:
        """Reconstruct a SentryContext from cached dict."""
        return SentryContext(
            issues=[
                SentryIssue(
                    title=i.get("title", ""),
                    culprit=i.get("culprit", ""),
                    count=i.get("count", 0),
                    user_count=i.get("user_count", 0),
                    first_seen=i.get("first_seen", ""),
                    last_seen=i.get("last_seen", ""),
                    short_id=i.get("short_id", ""),
                )
                for i in data.get("issues", [])
            ],
            project_slug=data.get("project_slug", ""),
            file_paths_queried=data.get("file_paths_queried", []),
        )
