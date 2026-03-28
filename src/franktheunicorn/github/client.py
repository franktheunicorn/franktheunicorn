"""
GitHub API client using httpx.

Supports both real GitHub polling via token and a mock/demo mode
driven by local fixture JSON, so the app is usable and testable
without external API access.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


class GitHubClient:
    """Thin wrapper around the GitHub REST API using httpx."""

    def __init__(self, token: str = "", base_url: str = GITHUB_API_BASE) -> None:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=30.0,
        )

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        """Fetch open pull requests for a repository."""
        url = f"/repos/{owner}/{repo}/pulls"
        response = self._client.get(url, params={"state": state, "per_page": 50})
        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()
        return result

    def get_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """Fetch the list of files changed in a PR."""
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
        response = self._client.get(url, params={"per_page": 100})
        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()
        return result

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Fetch the diff for a PR."""
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        response = self._client.get(url, headers={"Accept": "application/vnd.github.v3.diff"})
        response.raise_for_status()
        return response.text

    def close(self) -> None:
        self._client.close()
