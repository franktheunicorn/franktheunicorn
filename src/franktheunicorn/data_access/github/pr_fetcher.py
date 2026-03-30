"""Dual-path fetcher for pull request summaries."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    DataFetcher,
    FetchMethod,
    NotFoundError,
    RateLimitError,
)
from franktheunicorn.data_access.github.types import PRFileChange, PRSummary
from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_WEB_BASE = "https://github.com"


def _api_json_to_pr_summary(
    pr_json: dict[str, Any],
    files_json: list[dict[str, Any]],
    method: FetchMethod,
) -> PRSummary:
    """Map GitHub API JSON to a PRSummary dataclass."""
    files = tuple(
        PRFileChange(
            filename=f.get("filename", ""),
            status=f.get("status", "modified"),
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            patch=f.get("patch", ""),
        )
        for f in files_json
    )
    labels = tuple(
        label["name"] if isinstance(label, dict) else str(label)
        for label in pr_json.get("labels", [])
    )
    reviewers = tuple(
        r["login"] if isinstance(r, dict) else str(r)
        for r in pr_json.get("requested_reviewers", [])
    )
    user = pr_json.get("user", {})
    return PRSummary(
        fetched_via=method,
        number=pr_json.get("number", 0),
        title=pr_json.get("title", ""),
        author=user.get("login", "") if isinstance(user, dict) else str(user),
        state=pr_json.get("state", "open"),
        url=pr_json.get("html_url", ""),
        diff_url=pr_json.get("diff_url", ""),
        body=pr_json.get("body", "") or "",
        labels=labels,
        requested_reviewers=reviewers,
        is_draft=pr_json.get("draft", False),
        created_at=pr_json.get("created_at", ""),
        updated_at=pr_json.get("updated_at", ""),
        additions=pr_json.get("additions", 0),
        deletions=pr_json.get("deletions", 0),
        files=files,
    )


class PRFetcherAPI(DataFetcher[PRSummary]):
    """Fetch a PR summary via the GitHub REST API."""

    def __init__(
        self,
        client: httpx.Client,
        rate_limiter: GitHubRateLimiter | None = None,
    ) -> None:
        super().__init__(client, rate_limiter)

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRSummary:
        if self._rate_limiter is not None and isinstance(self._rate_limiter, GitHubRateLimiter):
            if self._rate_limiter.is_rate_limited():
                raise RateLimitError("GitHub API rate limited", method=FetchMethod.API)
            self._rate_limiter.acquire()

        # Fetch PR metadata
        pr_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        pr_resp = self._client.get(pr_url, headers={"Accept": "application/vnd.github+json"})

        if self._rate_limiter is not None and isinstance(self._rate_limiter, GitHubRateLimiter):
            self._rate_limiter.update_from_headers(pr_resp.headers)

        if pr_resp.status_code == 404:
            raise NotFoundError(
                f"PR #{pr_number} not found in {owner}/{repo}",
                method=FetchMethod.API,
                status_code=404,
            )
        if pr_resp.status_code in (403, 429):
            raise RateLimitError(
                f"Rate limited ({pr_resp.status_code})",
                method=FetchMethod.API,
                status_code=pr_resp.status_code,
            )
        pr_resp.raise_for_status()
        pr_json: dict[str, Any] = pr_resp.json()

        # Fetch changed files
        files_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        files_resp = self._client.get(
            files_url,
            headers={"Accept": "application/vnd.github+json"},
            params={"per_page": 100},
        )

        if self._rate_limiter is not None and isinstance(self._rate_limiter, GitHubRateLimiter):
            self._rate_limiter.update_from_headers(files_resp.headers)

        files_resp.raise_for_status()
        files_json: list[dict[str, Any]] = files_resp.json()

        return _api_json_to_pr_summary(pr_json, files_json, FetchMethod.API)

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRSummary:
        return _fetch_pr_via_scrape(self._client, owner, repo, pr_number)


class PRFetcherScrape(DataFetcher[PRSummary]):
    """Fetch a PR summary by scraping the GitHub web page."""

    def __init__(
        self,
        client: httpx.Client,
        rate_limiter: GitHubRateLimiter | None = None,
    ) -> None:
        super().__init__(client, rate_limiter)

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRSummary:
        raise RateLimitError("Scrape-only fetcher", method=FetchMethod.API)

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRSummary:
        return _fetch_pr_via_scrape(self._client, owner, repo, pr_number)


def _fetch_pr_via_scrape(client: httpx.Client, owner: str, repo: str, pr_number: int) -> PRSummary:
    """Scrape the GitHub PR page for summary data."""
    url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/pull/{pr_number}"
    response = client.get(url)

    if response.status_code == 404:
        raise NotFoundError(
            f"PR #{pr_number} page not found",
            method=FetchMethod.SCRAPE,
            status_code=404,
        )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Title: usually in <bdi> inside the PR title heading, or <h1>
    title_el = soup.select_one(".gh-header-title .js-issue-title")
    if title_el is None:
        title_el = soup.select_one("h1 bdi")
    title = title_el.get_text(strip=True) if title_el else f"PR #{pr_number}"

    # Author
    author_el = soup.select_one(".gh-header-meta .author")
    if author_el is None:
        author_el = soup.select_one("a.author")
    author = author_el.get_text(strip=True) if author_el else ""

    # State (open/closed/merged)
    state = "open"
    state_el = soup.select_one(".State")
    if state_el:
        state_text = state_el.get_text(strip=True).lower()
        if "merged" in state_text:
            state = "merged"
        elif "closed" in state_text:
            state = "closed"

    # Labels
    label_els = soup.select(".sidebar-labels .IssueLabel")
    labels = tuple(el.get_text(strip=True) for el in label_els)

    # Draft
    is_draft = "Draft" in (state_el.get_text(strip=True) if state_el else "")

    # Body
    body_el = soup.select_one(".comment-body")
    body = body_el.get_text(strip=True) if body_el else ""

    return PRSummary(
        fetched_via=FetchMethod.SCRAPE,
        number=pr_number,
        title=title,
        author=author,
        state=state,
        url=url,
        diff_url=f"{url}.diff",
        body=body,
        labels=labels,
        requested_reviewers=(),  # not reliably available from HTML
        is_draft=is_draft,
    )
