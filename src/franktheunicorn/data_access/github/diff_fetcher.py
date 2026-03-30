"""Dual-path fetcher for PR diffs."""

from __future__ import annotations

import re

from franktheunicorn.data_access.base import (
    GITHUB_API_BASE,
    GITHUB_WEB_BASE,
    DataFetcher,
    FetchMethod,
    NotFoundError,
)
from franktheunicorn.data_access.github.types import PRDiff, PRFileChange

# Matches "diff --git a/path b/path" headers in unified diffs
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


def parse_unified_diff(raw: str) -> tuple[PRFileChange, ...]:
    """Parse a unified diff string into per-file change records."""
    sections = _DIFF_HEADER_RE.split(raw)
    if len(sections) < 4:
        return ()

    files: list[PRFileChange] = []
    # sections layout: [preamble, a_path, b_path, chunk, a_path, b_path, chunk, ...]
    i = 1
    while i + 2 < len(sections):
        a_path = sections[i]
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
        elif a_path != b_path:
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


class DiffFetcher(DataFetcher[PRDiff]):
    """Fetch PR diff via API or by scraping the .diff URL."""

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRDiff:
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        response = self._api_get(url, headers={"Accept": "application/vnd.github.v3.diff"})
        return _build_diff(pr_number, response.text, FetchMethod.API)

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRDiff:
        url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/pull/{pr_number}.diff"
        response = self._client.get(url)
        if response.status_code == 404:
            raise NotFoundError(
                f"PR #{pr_number} diff not found",
                method=FetchMethod.SCRAPE,
                status_code=404,
            )
        response.raise_for_status()
        return _build_diff(pr_number, response.text, FetchMethod.SCRAPE)


def _build_diff(pr_number: int, raw_diff: str, method: FetchMethod) -> PRDiff:
    return PRDiff(
        fetched_via=method,
        pr_number=pr_number,
        raw_diff=raw_diff,
        files=parse_unified_diff(raw_diff),
    )
