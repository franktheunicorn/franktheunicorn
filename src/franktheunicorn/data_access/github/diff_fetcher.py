"""Dual-path fetcher for PR diffs."""

from __future__ import annotations

from unidiff import PatchSet  # type: ignore[import-untyped]

from franktheunicorn.data_access.base import (
    GITHUB_API_BASE,
    GITHUB_WEB_BASE,
    DataFetcher,
    FetchMethod,
)
from franktheunicorn.data_access.github.types import PRDiff, PRFileChange


def _detect_status(pf: object) -> str:
    """Map unidiff's patched-file flags to our status string."""
    if getattr(pf, "is_added_file", False):
        return "added"
    if getattr(pf, "is_removed_file", False):
        return "removed"
    if getattr(pf, "is_rename", False):
        return "renamed"
    return "modified"


def parse_unified_diff(raw: str) -> tuple[PRFileChange, ...]:
    """Parse a unified diff string into per-file change records."""
    try:
        patch_set = PatchSet(raw)
    except Exception:
        return ()
    return tuple(
        PRFileChange(
            filename=pf.path,
            status=_detect_status(pf),
            additions=pf.added,
            deletions=pf.removed,
            patch=str(pf).strip(),
        )
        for pf in patch_set
    )


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
        response = self._scrape_get(url)
        return _build_diff(pr_number, response.text, FetchMethod.SCRAPE)


def _build_diff(pr_number: int, raw_diff: str, method: FetchMethod) -> PRDiff:
    return PRDiff(
        fetched_via=method,
        pr_number=pr_number,
        raw_diff=raw_diff,
        files=parse_unified_diff(raw_diff),
    )
