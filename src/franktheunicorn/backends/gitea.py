"""
Gitea / Forgejo API client.

Forgejo is a Gitea fork that maintains API compatibility, so a single
client serves both. Endpoints live under ``/api/v1`` on the instance's
base URL. Authentication uses ``Authorization: token <pat>`` (not
``Bearer``). Inline review comments use diff-position offsets, computed
on the fly from the PR diff via ``diff_position.translate_line_to_position``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from franktheunicorn.backends.base import ForgeClient, ReviewBody, ReviewComment
from franktheunicorn.backends.diff_position import translate_line_to_position

logger = logging.getLogger(__name__)


def _normalize_base_url(base_url: str) -> str:
    """Ensure base_url ends with ``/api/v1``.

    Operators commonly write ``https://codeberg.org`` in their config; the
    REST API actually lives at ``/api/v1``. Append it if missing.
    """
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/api/v1"):
        base_url = base_url + "/api/v1"
    return base_url


class GiteaClient(ForgeClient):
    """ForgeClient backed by the Gitea/Forgejo REST API."""

    def __init__(self, token: str = "", base_url: str = "") -> None:
        if not base_url:
            msg = "GiteaClient requires base_url (e.g. https://codeberg.org)"
            raise ValueError(msg)
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"token {token}"
        self._client = httpx.Client(
            base_url=_normalize_base_url(base_url),
            headers=headers,
            timeout=30.0,
        )
        # Public web URL (no /api/v1) used to synthesize html_url/diff_url
        # when the API response omits them.
        self._web_base = base_url.rstrip("/")
        if self._web_base.endswith("/api/v1"):
            self._web_base = self._web_base[: -len("/api/v1")]

    # ------------------------------------------------------------------
    # Pull request discovery
    # ------------------------------------------------------------------

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        url = f"/repos/{owner}/{repo}/pulls"
        response = self._client.get(url, params={"state": state, "limit": 50})
        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()
        for pr in result:
            self._normalize_pr_shape(pr, owner, repo)
        return result

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        response = self._client.get(url)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        self._normalize_pr_shape(result, owner, repo)
        return result

    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
        response = self._client.get(url, params={"limit": 100})
        response.raise_for_status()
        files: list[dict[str, Any]] = response.json()
        # Gitea returns ``filename`` already, matching the GitHub field.
        return files

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        # Gitea exposes the raw diff at ``/repos/{owner}/{repo}/pulls/{idx}.diff``
        # outside the JSON Accept negotiation.
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}.diff"
        response = self._client.get(url, headers={"Accept": "text/plain"})
        response.raise_for_status()
        return response.text

    # ------------------------------------------------------------------
    # Review create / fetch / delete
    # ------------------------------------------------------------------

    def create_review(
        self, owner: str, repo: str, pr_number: int, review: ReviewBody
    ) -> dict[str, Any]:
        """Submit a review with line→position translation for inline comments.

        Comments whose line cannot be located in the diff are dropped with
        a warning rather than failing the whole submission.
        """
        diff_text = ""
        if any(c.line is not None for c in review.comments):
            try:
                diff_text = self.get_pull_request_diff(owner, repo, pr_number)
            except Exception:
                logger.warning(
                    "Could not fetch diff for %s/%s#%d; inline comments will be dropped",
                    owner,
                    repo,
                    pr_number,
                )

        # Track the source position of each posted comment so we can map
        # fetched comment IDs back into a 1:1-aligned ``comment_ids`` list.
        wire_comments: list[dict[str, Any]] = []
        posted_indices: list[int] = []
        for idx, comment in enumerate(review.comments):
            wire = _to_gitea_comment(comment, diff_text)
            if wire is None:
                logger.warning(
                    "Dropping inline comment on %s:%s — line not located in diff",
                    comment.path,
                    comment.line,
                )
                continue
            wire_comments.append(wire)
            posted_indices.append(idx)

        payload: dict[str, Any] = {"event": review.event}
        if review.body:
            payload["body"] = review.body
        payload["comments"] = wire_comments

        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        result: dict[str, Any] = response.json()

        comment_ids: list[int | None] = [None] * len(review.comments)
        review_id = result.get("id")
        if review_id and wire_comments:
            try:
                posted_comments = self.get_review_comments(owner, repo, pr_number, review_id)
                fetched_ids = [c["id"] for c in posted_comments if "id" in c]
                for fetched_idx, source_idx in enumerate(posted_indices):
                    if fetched_idx < len(fetched_ids):
                        comment_ids[source_idx] = fetched_ids[fetched_idx]
            except Exception:
                logger.warning(
                    "Could not fetch posted comment IDs for %s/%s#%d review %s",
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
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}/comments"
        response = self._client.get(url)
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
        url = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        params: dict[str, str | int] = {"limit": 100}
        if since:
            params["since"] = since
        response = self._client.get(url, params=params)
        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()
        return result

    def delete_review_comment(self, owner: str, repo: str, pr_number: int, comment_id: int) -> None:
        """Delete a posted review comment. ``pr_number`` is unused on Gitea/Forgejo.

        Some older Forgejo versions may not support this URL form; recall
        is best-effort.
        """
        del pr_number
        url = f"/repos/{owner}/{repo}/pulls/comments/{comment_id}"
        response = self._client.delete(url)
        response.raise_for_status()

    def get_authenticated_user(self) -> dict[str, Any]:
        response = self._client.get("/user")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize_pr_shape(self, pr: dict[str, Any], owner: str, repo: str) -> None:
        """Paper over PR-shape differences so the poller is forge-blind.

        - Ensures ``diff_url`` is set (Gitea omits it; synthesize from html_url).
        - Maps Gitea's ``draft`` field name through unchanged (it matches GitHub).
        """
        if "diff_url" not in pr or not pr.get("diff_url"):
            html_url = pr.get("html_url") or (
                f"{self._web_base}/{owner}/{repo}/pulls/{pr.get('number', 0)}"
            )
            pr["diff_url"] = html_url + ".diff"


def _to_gitea_comment(comment: ReviewComment, diff_text: str) -> dict[str, Any] | None:
    """Convert a normalized ReviewComment to Gitea's wire format.

    Returns ``None`` if the comment has a line number that cannot be
    located in the diff — caller is responsible for logging and dropping.
    """
    out: dict[str, Any] = {"path": comment.path, "body": comment.body}
    if comment.line is None:
        # File-level / general comment without a position.
        return out

    target_line = comment.line_end if comment.line_end else comment.line
    position = translate_line_to_position(diff_text, comment.path, target_line, comment.side)
    if position is None:
        return None
    if comment.side == "LEFT":
        out["old_position"] = position
    else:
        out["new_position"] = position
    return out
