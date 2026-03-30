"""Dual-path fetcher for pull request summaries."""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    GITHUB_API_BASE,
    GITHUB_WEB_BASE,
    DataFetcher,
    FetchMethod,
)
from franktheunicorn.data_access.github.types import PRFileChange, PRSummary


class PRFetcher(DataFetcher[PRSummary]):
    """Fetch a PR summary via API or by scraping the GitHub web page."""

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRSummary:
        pr_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        pr_resp = self._api_get(pr_url, headers={"Accept": "application/vnd.github+json"})
        pr_json: dict[str, Any] = pr_resp.json()

        files_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        files_resp = self._api_get(
            files_url,
            headers={"Accept": "application/vnd.github+json"},
            params={"per_page": 100},
        )
        files_json: list[dict[str, Any]] = files_resp.json()

        return _api_json_to_pr_summary(pr_json, files_json)

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRSummary:
        url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/pull/{pr_number}"
        response = self._scrape_get(url)

        soup = BeautifulSoup(response.text, "html.parser")

        title_el = soup.select_one(".gh-header-title .js-issue-title") or soup.select_one("h1 bdi")
        title = title_el.get_text(strip=True) if title_el else f"PR #{pr_number}"

        author_el = soup.select_one(".gh-header-meta .author") or soup.select_one("a.author")
        author = author_el.get_text(strip=True) if author_el else ""

        state = "open"
        state_el = soup.select_one(".State")
        if state_el:
            state_text = state_el.get_text(strip=True).lower()
            if "merged" in state_text:
                state = "merged"
            elif "closed" in state_text:
                state = "closed"

        labels = tuple(
            el.get_text(strip=True) for el in soup.select(".sidebar-labels .IssueLabel")
        )
        is_draft = "Draft" in (state_el.get_text(strip=True) if state_el else "")

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
            requested_reviewers=(),
            is_draft=is_draft,
        )


def _api_json_to_pr_summary(
    pr_json: dict[str, Any],
    files_json: list[dict[str, Any]],
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
        fetched_via=FetchMethod.API,
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
