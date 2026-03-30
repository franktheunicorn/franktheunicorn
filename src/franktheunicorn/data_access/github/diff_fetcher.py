"""Dual-path fetcher for PR diffs."""

from __future__ import annotations

import logging
import re

import httpx

from franktheunicorn.data_access.base import (
    DataFetcher,
    FetchMethod,
    NotFoundError,
    RateLimitError,
)
from franktheunicorn.data_access.github.types import PRDiff, PRFileChange
from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_WEB_BASE = "https://github.com"

# Matches "diff --git a/path b/path" headers in unified diffs
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_STAT_RE = re.compile(r"^@@\s", re.MULTILINE)


def _parse_unified_diff(raw: str) -> tuple[PRFileChange, ...]:
    """Parse a unified diff string into per-file change records."""
    sections = _DIFF_HEADER_RE.split(raw)
    if len(sections) < 4:
        return ()

    files: list[PRFileChange] = []
    # sections[0] is text before first diff header (usually empty)
    # then groups of 3: (a_path, b_path, chunk_text)
    i = 1
    while i + 2 < len(sections):
        _a_path = sections[i]
        b_path = sections[i + 1]
        chunk = sections[i + 2]

        additions = chunk.count("\n+") - chunk.count("\n+++")
        deletions = chunk.count("\n-") - chunk.count("\n---")

        # Detect add/remove from --- /dev/null or +++ /dev/null in the chunk
        has_dev_null_old = "--- /dev/null" in chunk
        has_dev_null_new = "+++ /dev/null" in chunk

        if has_dev_null_old and not has_dev_null_new:
            status = "added"
        elif not has_dev_null_old and has_dev_null_new:
            status = "removed"
        elif _a_path != b_path:
            status = "renamed"
        else:
            status = "modified"

        files.append(
            PRFileChange(
                filename=b_path,
                status=status,
                additions=max(0, additions),
                deletions=max(0, deletions),
                patch=chunk.strip(),
            )
        )
        i += 3

    return tuple(files)


class DiffFetcherAPI(DataFetcher[PRDiff]):
    """Fetch PR diff via the GitHub REST API."""

    def __init__(
        self,
        client: httpx.Client,
        rate_limiter: GitHubRateLimiter | None = None,
    ) -> None:
        super().__init__(client, rate_limiter)

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRDiff:
        if self._rate_limiter is not None and isinstance(self._rate_limiter, GitHubRateLimiter):
            if self._rate_limiter.is_rate_limited():
                raise RateLimitError("GitHub API rate limited", method=FetchMethod.API)
            self._rate_limiter.acquire()

        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        response = self._client.get(url, headers={"Accept": "application/vnd.github.v3.diff"})

        if self._rate_limiter is not None and isinstance(self._rate_limiter, GitHubRateLimiter):
            self._rate_limiter.update_from_headers(response.headers)

        if response.status_code == 404:
            raise NotFoundError(
                f"PR #{pr_number} not found in {owner}/{repo}",
                method=FetchMethod.API,
                status_code=404,
            )
        if response.status_code in (403, 429):
            raise RateLimitError(
                f"Rate limited ({response.status_code})",
                method=FetchMethod.API,
                status_code=response.status_code,
            )
        response.raise_for_status()

        raw_diff = response.text
        files = _parse_unified_diff(raw_diff)
        return PRDiff(
            fetched_via=FetchMethod.API,
            pr_number=pr_number,
            raw_diff=raw_diff,
            files=files,
        )

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRDiff:
        return _fetch_diff_via_scrape(self._client, owner, repo, pr_number)


class DiffFetcherScrape(DataFetcher[PRDiff]):
    """Fetch PR diff by scraping the .diff URL."""

    def __init__(
        self,
        client: httpx.Client,
        rate_limiter: GitHubRateLimiter | None = None,
    ) -> None:
        super().__init__(client, rate_limiter)

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRDiff:
        raise RateLimitError("Scrape-only fetcher", method=FetchMethod.API)

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRDiff:
        return _fetch_diff_via_scrape(self._client, owner, repo, pr_number)


def _fetch_diff_via_scrape(client: httpx.Client, owner: str, repo: str, pr_number: int) -> PRDiff:
    """Fetch the .diff URL (public, no auth required)."""
    url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/pull/{pr_number}.diff"
    response = client.get(url)

    if response.status_code == 404:
        raise NotFoundError(
            f"PR #{pr_number} diff not found",
            method=FetchMethod.SCRAPE,
            status_code=404,
        )
    response.raise_for_status()

    raw_diff = response.text
    files = _parse_unified_diff(raw_diff)
    return PRDiff(
        fetched_via=FetchMethod.SCRAPE,
        pr_number=pr_number,
        raw_diff=raw_diff,
        files=files,
    )
