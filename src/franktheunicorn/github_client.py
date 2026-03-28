"""GitHub API client backed by httpx.

Operator-in-the-loop: we only READ from GitHub here.
Writing (posting reviews) is a future extension and off by default.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import httpx

from franktheunicorn.config import get_settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0


class GitHubPR:
    """Lightweight value object for a GitHub pull request response."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def number(self) -> int:
        return int(self._data.get("number", 0))

    @property
    def title(self) -> str:
        return str(self._data.get("title", ""))

    @property
    def author_login(self) -> str:
        return str(self._data.get("user", {}).get("login", ""))

    @property
    def state(self) -> str:
        return str(self._data.get("state", "open"))

    @property
    def html_url(self) -> str:
        return str(self._data.get("html_url", ""))

    @property
    def body(self) -> str:
        return str(self._data.get("body") or "")

    @property
    def labels(self) -> list[str]:
        return [lbl["name"] for lbl in self._data.get("labels", [])]

    @property
    def requested_reviewers(self) -> list[str]:
        return [r["login"] for r in self._data.get("requested_reviewers", [])]

    @property
    def created_at(self) -> datetime.datetime | None:
        raw = self._data.get("created_at")
        if raw:
            return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return None

    @property
    def updated_at(self) -> datetime.datetime | None:
        raw = self._data.get("updated_at")
        if raw:
            return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return None

    @property
    def raw(self) -> dict[str, Any]:
        return self._data


class GitHubClient:
    """Thin httpx wrapper for the GitHub REST API."""

    def __init__(self, token: str | None = None) -> None:
        resolved_token = token or get_settings().github_token
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"
        else:
            logger.warning("No GitHub token configured - rate limits will be tight.")
        self._client = httpx.Client(
            base_url=GITHUB_API_BASE,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def list_open_prs(self, repo: str, per_page: int = 50) -> list[GitHubPR]:
        """Return open pull requests for *repo* (e.g. 'apache/spark')."""
        url = f"/repos/{repo}/pulls"
        params: dict[str, str | int] = {"state": "open", "per_page": per_page, "sort": "updated"}
        try:
            resp = self._client.get(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("GitHub API error for %s: %s", repo, exc)
            return []
        except httpx.RequestError as exc:
            logger.error("Network error polling %s: %s", repo, exc)
            return []
        return [GitHubPR(item) for item in resp.json()]

    def list_pr_files(self, repo: str, pr_number: int) -> list[str]:
        """Return changed file paths for a specific PR."""
        url = f"/repos/{repo}/pulls/{pr_number}/files"
        try:
            resp = self._client.get(url, params={"per_page": 100})
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("Error fetching files for %s#%d: %s", repo, pr_number, exc)
            return []
        return [item["filename"] for item in resp.json()]

    def get_issue_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Return issue-level comments on a PR (mentions live here)."""
        url = f"/repos/{repo}/issues/{pr_number}/comments"
        try:
            resp = self._client.get(url, params={"per_page": 100})
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("Error fetching comments for %s#%d: %s", repo, pr_number, exc)
            return []
        return list(resp.json())

    def get_contributors(self, repo: str, per_page: int = 100) -> list[str]:
        """Return contributor logins for *repo* (used for scoring)."""
        url = f"/repos/{repo}/contributors"
        try:
            resp = self._client.get(url, params={"per_page": per_page})
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("Error fetching contributors for %s: %s", repo, exc)
            return []
        return [item["login"] for item in resp.json()]
