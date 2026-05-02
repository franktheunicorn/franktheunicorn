"""
GitHub API client using httpx.

Implements the ``ForgeClient`` ABC. ``create_review`` accepts the
forge-agnostic ``ReviewBody`` dataclass and converts to GitHub's wire
format internally, so callers can target any forge uniformly.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from franktheunicorn.backends.base import ForgeClient, ReviewBody, ReviewComment, infer_username
from franktheunicorn.data_access.base import GITHUB_API_BASE

logger = logging.getLogger(__name__)


class GitHubClient(ForgeClient):
    """ForgeClient implementation backed by the GitHub REST API."""

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
        self, owner: str, repo: str, pr_number: int, review: ReviewBody
    ) -> dict[str, Any]:
        """Create a pull request review with comments.

        Converts the forge-agnostic ``ReviewBody`` to GitHub's wire
        format and populates ``comment_ids`` on the result by querying
        the review's comments after creation. GitHub returns review
        comments in posting order, so the IDs align with ``review.comments``.
        """
        payload: dict[str, Any] = {"event": review.event}
        if review.body:
            payload["body"] = review.body
        payload["comments"] = [_to_github_comment(c) for c in review.comments]

        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        result: dict[str, Any] = response.json()

        # 1:1 list aligned with review.comments. None entries flag
        # comments whose ID could not be retrieved (we never drop
        # GitHub-side; the server validates the whole submission).
        comment_ids: list[int | None] = [None] * len(review.comments)
        review_id = result.get("id")
        if review_id and review.comments:
            try:
                posted_comments = self.get_review_comments(owner, repo, pr_number, review_id)
                fetched_ids = [c["id"] for c in posted_comments if "id" in c]
                for i, fid in enumerate(fetched_ids):
                    if i < len(comment_ids):
                        comment_ids[i] = fid
            except Exception:
                logger.warning(
                    "Could not fetch posted comment IDs for %s/%s#%d review %d",
                    owner,
                    repo,
                    pr_number,
                    review_id,
                )
        result["comment_ids"] = comment_ids
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

    def delete_review_comment(self, owner: str, repo: str, pr_number: int, comment_id: int) -> None:
        """Delete a review comment (for recall). ``pr_number`` is unused on GitHub."""
        del pr_number
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


def _to_github_comment(comment: ReviewComment) -> dict[str, Any]:
    """Convert a normalized ReviewComment to GitHub's review-comment wire format."""
    out: dict[str, Any] = {"path": comment.path, "body": comment.body}
    if comment.line is not None:
        out["line"] = comment.line
        out["side"] = comment.side
        if comment.line_end is not None and comment.line_end > comment.line:
            out["start_line"] = comment.line
            out["line"] = comment.line_end
    return out


def infer_github_username(token: str, base_url: str = GITHUB_API_BASE) -> str:
    """Infer the GitHub username from a personal access token.

    Back-compat wrapper around ``infer_username``. New code should prefer
    constructing the appropriate ``ForgeClient`` and calling
    ``infer_username`` directly.
    """
    if not token:
        return ""
    client = GitHubClient(token=token, base_url=base_url)
    try:
        return infer_username(client)
    finally:
        client.close()
