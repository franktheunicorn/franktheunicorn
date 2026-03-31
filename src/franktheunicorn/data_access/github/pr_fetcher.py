"""Dual-path fetcher for pull request summaries."""

from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    GITHUB_API_BASE,
    GITHUB_WEB_BASE,
    DataFetcher,
    FetchMethod,
    get_login,
)
from franktheunicorn.data_access.github.types import PRFileChange, PRSummary

logger = logging.getLogger(__name__)


class PRFetcher(DataFetcher[PRSummary]):
    """Fetch a PR summary via API or by scraping the GitHub web page."""

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRSummary:
        base = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        pr_json: dict[str, Any] = self._api_get_json(base).json()
        files_json: list[dict[str, Any]] = self._api_get_json(f"{base}/files", per_page=100).json()
        return _api_json_to_pr_summary(pr_json, files_json)

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRSummary:
        url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/pull/{pr_number}"
        soup = BeautifulSoup(self._scrape_get(url).text, "html.parser")

        title_el = soup.select_one(".gh-header-title .js-issue-title") or soup.select_one("h1 bdi")
        author_el = soup.select_one(".gh-header-meta .author") or soup.select_one("a.author")
        state_el = soup.select_one(".State")
        body_el = soup.select_one(".comment-body")

        state = "open"
        if state_el:
            state_text = state_el.get_text(strip=True).lower()
            if "merged" in state_text:
                state = "merged"
            elif "closed" in state_text:
                state = "closed"

        # Best-effort merge conflict detection from the merge box.
        # GitHub shows "This branch has conflicts" or a green merge button.
        try:
            mergeable = _scrape_mergeable(soup)
        except MergeStatusParseError:
            logger.warning(
                "Could not parse merge status for %s/%s#%d from HTML; "
                "GitHub's HTML structure may have changed.",
                owner,
                repo,
                pr_number,
            )
            mergeable = None

        return PRSummary(
            fetched_via=FetchMethod.SCRAPE,
            number=pr_number,
            title=title_el.get_text(strip=True) if title_el else f"PR #{pr_number}",
            author=author_el.get_text(strip=True) if author_el else "",
            state=state,
            url=url,
            diff_url=f"{url}.diff",
            body=body_el.get_text(strip=True) if body_el else "",
            labels=tuple(
                el.get_text(strip=True) for el in soup.select(".sidebar-labels .IssueLabel")
            ),
            requested_reviewers=(),
            is_draft="Draft" in (state_el.get_text(strip=True) if state_el else ""),
            mergeable=mergeable,
        )


class MergeStatusParseError(Exception):
    """Raised when we can't determine merge status from the HTML."""


def _scrape_mergeable(soup: BeautifulSoup) -> bool | None:
    """Best-effort extraction of mergeable status from the PR page HTML.

    GitHub's merge box contains text like:
    - "This branch has conflicts that must be resolved"
    - "This branch has no conflicts with the base branch"
    - "Merging can be performed automatically"

    Returns True (mergeable), False (conflicts), or None (can't determine).
    Raises MergeStatusParseError if the merge box is found but unparseable.
    """
    # Look for the merge status area.
    merge_box = soup.select_one(".merge-message, .merging-body, [data-merge-box]")
    if merge_box is None:
        return None  # No merge box found (e.g., closed PR, or HTML changed)

    text = merge_box.get_text(" ", strip=True).lower()
    if not text:
        return None

    if "has conflicts" in text or "resolve conflicts" in text or "can't be merged" in text:
        return False
    if (
        "no conflicts" in text
        or "can be performed automatically" in text
        or "able to merge" in text
        or "merge pull request" in text
    ):
        return True

    # Found a merge box but couldn't parse it — raise so caller knows
    # the HTML structure may have changed.
    raise MergeStatusParseError(
        f"Found merge box but could not determine status from: {text[:200]}"
    )


def _api_json_to_pr_summary(
    pr_json: dict[str, Any],
    files_json: list[dict[str, Any]],
) -> PRSummary:
    """Map GitHub API JSON to a PRSummary dataclass."""
    # GitHub returns mergeable as true/false/null. null means "not yet computed"
    # which we treat the same as None (unknown).
    raw_mergeable = pr_json.get("mergeable")
    mergeable: bool | None = None
    if isinstance(raw_mergeable, bool):
        mergeable = raw_mergeable

    return PRSummary(
        fetched_via=FetchMethod.API,
        number=pr_json.get("number", 0),
        title=pr_json.get("title", ""),
        author=get_login(pr_json),
        state=pr_json.get("state", "open"),
        url=pr_json.get("html_url", ""),
        diff_url=pr_json.get("diff_url", ""),
        body=pr_json.get("body", "") or "",
        labels=tuple(
            label["name"] if isinstance(label, dict) else str(label)
            for label in pr_json.get("labels", [])
        ),
        requested_reviewers=tuple(
            r["login"] if isinstance(r, dict) else str(r)
            for r in pr_json.get("requested_reviewers", [])
        ),
        is_draft=pr_json.get("draft", False),
        created_at=pr_json.get("created_at", ""),
        updated_at=pr_json.get("updated_at", ""),
        additions=pr_json.get("additions", 0),
        deletions=pr_json.get("deletions", 0),
        files=tuple(
            PRFileChange(
                filename=f.get("filename", ""),
                status=f.get("status", "modified"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                patch=f.get("patch", ""),
            )
            for f in files_json
        ),
        mergeable=mergeable,
    )
