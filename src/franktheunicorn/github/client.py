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

from franktheunicorn.data_access.base import GITHUB_API_BASE

logger = logging.getLogger(__name__)


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

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch a single PR detail (includes mergeable status)."""
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        response = self._client.get(url)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
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

    def create_review(
        self, owner: str, repo: str, pr_number: int, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a pull request review with comments."""
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        response = self._client.post(url, json=body)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict[str, Any]]:
        """Fetch comments from a specific review."""
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}/comments"
        response = self._client.get(url, params={"per_page": 100})
        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()
        return result

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch conversation comments on a PR/issue.

        If *since* is provided (ISO 8601), only returns comments updated
        at or after that timestamp.
        """
        url = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        params: dict[str, str | int] = {"per_page": 100}
        if since:
            params["since"] = since
        response = self._client.get(url, params=params)
        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()
        return result

    def delete_review_comment(self, owner: str, repo: str, comment_id: int) -> None:
        """Delete a review comment (for recall)."""
        url = f"/repos/{owner}/{repo}/pulls/comments/{comment_id}"
        response = self._client.delete(url)
        response.raise_for_status()

    def get_authenticated_user(self) -> dict[str, Any]:
        """Fetch the authenticated user's profile (GET /user)."""
        response = self._client.get("/user")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def close(self) -> None:
        self._client.close()


def infer_github_username(token: str, base_url: str = GITHUB_API_BASE) -> str:
    """Infer the GitHub username from a personal access token.

    Calls ``GET /user`` and returns the ``login`` field.
    Returns an empty string if the request fails for any reason
    (network error, invalid token, insufficient scopes, etc.).
    """
    if not token:
        return ""
    client = GitHubClient(token=token, base_url=base_url)
    try:
        user_data = client.get_authenticated_user()
        login: str = user_data.get("login", "")
        if login:
            logger.info("Inferred GitHub username from token: %s", login)
        return login
    except Exception:
        logger.warning(
            "Could not infer GitHub username from token (network error or invalid token)",
            exc_info=True,
        )
        return ""
    finally:
        client.close()
