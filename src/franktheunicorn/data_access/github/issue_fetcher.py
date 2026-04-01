"""Dual-path fetcher for GitHub issues.

API path: GitHub REST API ``GET /repos/{owner}/{repo}/issues/{number}``
Scrape path: Parse GitHub issue page HTML

Both paths return a ``GitHubIssueResult`` with the same fields.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    GITHUB_API_BASE,
    GITHUB_WEB_BASE,
    DataFetcher,
    FetchMethod,
    get_login,
)
from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.github.issue_types import (
    GitHubIssueResult,
    IssueComment,
)

logger = logging.getLogger(__name__)

# Pattern to extract issue references from text.
# Matches: #123, org/repo#456
ISSUE_REF_PATTERN = re.compile(r"(?:([\w.-]+)/([\w.-]+))?#(\d+)")


class IssueFetcher(DataFetcher[GitHubIssueResult]):
    """Fetches GitHub issues via REST API or HTML scrape."""

    def __init__(
        self,
        *args: Any,
        cache: FileCache | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._cache = cache or FileCache(source_name="github_issues")

    def fetch_linked_issues(
        self,
        owner: str,
        repo: str,
        text: str,
    ) -> list[GitHubIssueResult]:
        """Parse issue references from text and fetch each via API.

        Recognizes ``#123`` (same repo) and ``org/repo#456`` (cross-repo).
        """
        matches = ISSUE_REF_PATTERN.findall(text)
        results: list[GitHubIssueResult] = []
        seen: set[tuple[str, str, int]] = set()

        for ref_owner, ref_repo, number_str in matches:
            issue_owner = ref_owner or owner
            issue_repo = ref_repo or repo
            issue_number = int(number_str)

            key = (issue_owner, issue_repo, issue_number)
            if key in seen:
                continue
            seen.add(key)

            try:
                result = self.fetch_via_api(issue_owner, issue_repo, issue_number)
                results.append(result)
            except Exception:
                logger.debug(
                    "Failed to fetch issue %s/%s#%d",
                    issue_owner,
                    issue_repo,
                    issue_number,
                    exc_info=True,
                )

        return results

    def fetch_related_issues(
        self,
        owner: str,
        repo: str,
        keywords: str,
    ) -> list[GitHubIssueResult]:
        """Search for related issues using the GitHub search API."""
        query = f"{keywords}+repo:{owner}/{repo}+is:issue"
        url = f"{GITHUB_API_BASE}/search/issues"
        response = self._api_get_json(url, q=query)
        data: dict[str, Any] = response.json()

        results: list[GitHubIssueResult] = []
        for item in data.get("items", []):
            results.append(self._parse_api_item(item))

        return results

    def fetch_via_api(  # type: ignore[override]
        self,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> GitHubIssueResult:
        """Fetch a single issue via the GitHub REST API."""
        cached = self._cache.get(owner, repo, str(issue_number))
        if cached is not None:
            return self._from_cache_dict(cached.data)

        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}"
        response = self._api_get_json(url)
        data: dict[str, Any] = response.json()

        # Fetch comments if the issue has any.
        comments: list[IssueComment] = []
        comment_count = data.get("comments", 0)
        if comment_count > 0:
            comments_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/comments"
            comments_resp = self._api_get_json(comments_url, per_page=5)
            for c in comments_resp.json():
                comments.append(
                    IssueComment(
                        author=get_login(c),
                        body=c.get("body", ""),
                    )
                )

        result = GitHubIssueResult(
            fetched_via=FetchMethod.API,
            number=data.get("number", issue_number),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            state=data.get("state", "open"),
            labels=[
                lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
                for lbl in data.get("labels", [])
            ],
            author=get_login(data),
            url=data.get("html_url", ""),
            comments=comments,
        )

        self._cache.put(owner, repo, str(issue_number), data=result.to_cache_dict())
        return result

    def fetch_via_scrape(  # type: ignore[override]
        self,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> GitHubIssueResult:
        """Fetch a single issue by scraping the GitHub HTML page."""
        url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/issues/{issue_number}"
        response = self._scrape_get(url)
        return self._parse_html(response.text, owner, repo, issue_number)

    @staticmethod
    def _parse_api_item(data: dict[str, Any]) -> GitHubIssueResult:
        """Parse a single issue from API JSON (search result or direct)."""
        return GitHubIssueResult(
            fetched_via=FetchMethod.API,
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            state=data.get("state", "open"),
            labels=[
                lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
                for lbl in data.get("labels", [])
            ],
            author=get_login(data),
            url=data.get("html_url", ""),
        )

    @staticmethod
    def _parse_html(
        html: str,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> GitHubIssueResult:
        """Parse GitHub issue HTML page into a GitHubIssueResult."""
        soup = BeautifulSoup(html, "html.parser")

        # Title
        title = ""
        title_el = soup.find("bdi", class_="js-issue-title")
        if title_el:
            title = title_el.get_text(strip=True)
        else:
            title_el = soup.find("span", class_="js-issue-title")
            if title_el:
                title = title_el.get_text(strip=True)

        # Author
        author = ""
        author_el = soup.find("a", class_="author")
        if author_el:
            author = author_el.get_text(strip=True)

        # State
        state = "open"
        state_el = soup.find("span", class_="State")
        if state_el:
            state_text = state_el.get_text(strip=True).lower()
            if "closed" in state_text:
                state = "closed"

        # Labels
        labels: list[str] = []
        label_els = soup.find_all("a", class_="IssueLabel")
        for lbl in label_els:
            label_text = lbl.get_text(strip=True)
            if label_text:
                labels.append(label_text)

        # Body
        body = ""
        body_el = soup.find("td", class_="comment-body")
        if body_el:
            body = body_el.get_text(strip=True)

        # Comments
        comments: list[IssueComment] = []
        comment_els = soup.find_all("div", class_="timeline-comment")
        for comment_el in comment_els[1:]:  # skip first (issue body)
            c_author_el = comment_el.find("a", class_="author")
            c_body_el = comment_el.find("td", class_="comment-body")
            if c_author_el and c_body_el:
                comments.append(
                    IssueComment(
                        author=c_author_el.get_text(strip=True),
                        body=c_body_el.get_text(strip=True),
                    )
                )

        issue_url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/issues/{issue_number}"

        return GitHubIssueResult(
            fetched_via=FetchMethod.SCRAPE,
            number=issue_number,
            title=title,
            body=body,
            state=state,
            labels=labels,
            author=author,
            url=issue_url,
            comments=comments,
        )

    @staticmethod
    def _from_cache_dict(data: dict[str, Any]) -> GitHubIssueResult:
        """Reconstruct a GitHubIssueResult from cached dict."""
        return GitHubIssueResult(
            fetched_via=FetchMethod.API,
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", ""),
            state=data.get("state", "open"),
            labels=data.get("labels", []),
            author=data.get("author", ""),
            url=data.get("url", ""),
            comments=[
                IssueComment(author=c["author"], body=c["body"]) for c in data.get("comments", [])
            ],
        )
