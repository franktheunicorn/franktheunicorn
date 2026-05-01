"""
GitLab API client.

Maps the forge-agnostic ``ForgeClient`` interface onto GitLab's REST
API v4. Some terminology is adapted on the fly:

- "Pull request" → "merge request" (MR). Frank's PR number is GitLab's
  ``iid`` (project-internal ID).
- The PR list/detail responses are normalized so the poller (which
  expects GitHub-shaped fields) doesn't need to know.
- Inline review comments become MR *discussions* with a position object
  carrying base/start/head SHAs and old/new line numbers — no diff
  positional offset translation needed.
- The general review body is posted as a top-level MR note.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from franktheunicorn.backends.base import ForgeClient, ReviewBody, ReviewComment

logger = logging.getLogger(__name__)


def _project_id(owner: str, repo: str) -> str:
    """URL-encode ``owner/repo`` for use as a GitLab project ID."""
    return quote(f"{owner}/{repo}", safe="")


def _normalize_base_url(base_url: str) -> str:
    """Ensure base_url ends with ``/api/v4``."""
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/api/v4"):
        base_url = base_url + "/api/v4"
    return base_url


class GitLabClient(ForgeClient):
    """ForgeClient backed by the GitLab REST API."""

    def __init__(self, token: str = "", base_url: str = "https://gitlab.com") -> None:
        if not base_url:
            base_url = "https://gitlab.com"
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        self._client = httpx.Client(
            base_url=_normalize_base_url(base_url),
            headers=headers,
            timeout=30.0,
        )
        self._web_base = base_url.rstrip("/")
        if self._web_base.endswith("/api/v4"):
            self._web_base = self._web_base[: -len("/api/v4")]

    # ------------------------------------------------------------------
    # Pull request discovery
    # ------------------------------------------------------------------

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        # GitLab uses "opened" rather than "open".
        gitlab_state = "opened" if state == "open" else state
        url = f"/projects/{_project_id(owner, repo)}/merge_requests"
        response = self._client.get(url, params={"state": gitlab_state, "per_page": 50})
        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()
        return [_normalize_mr(mr) for mr in result]

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        url = f"/projects/{_project_id(owner, repo)}/merge_requests/{pr_number}"
        response = self._client.get(url)
        response.raise_for_status()
        return _normalize_mr(response.json())

    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Return changed files in GitHub-shaped form: ``[{filename, status, ...}]``."""
        url = f"/projects/{_project_id(owner, repo)}/merge_requests/{pr_number}/changes"
        response = self._client.get(url)
        response.raise_for_status()
        body = response.json()
        changes = body.get("changes", []) or []
        files: list[dict[str, Any]] = []
        for c in changes:
            new_path = c.get("new_path") or c.get("old_path", "")
            status = "modified"
            if c.get("new_file"):
                status = "added"
            elif c.get("deleted_file"):
                status = "removed"
            elif c.get("renamed_file"):
                status = "renamed"
            files.append({"filename": new_path, "status": status})
        return files

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Stitch a unified diff from GitLab's per-file diff hunks.

        GitLab doesn't expose a single ``.diff`` endpoint; we synthesize
        one so callers get a familiar text blob.
        """
        url = f"/projects/{_project_id(owner, repo)}/merge_requests/{pr_number}/changes"
        response = self._client.get(url)
        response.raise_for_status()
        body = response.json()
        changes = body.get("changes", []) or []
        parts: list[str] = []
        for c in changes:
            old_path = c.get("old_path", "")
            new_path = c.get("new_path", "")
            parts.append(f"diff --git a/{old_path} b/{new_path}\n")
            if c.get("new_file"):
                parts.append("--- /dev/null\n")
                parts.append(f"+++ b/{new_path}\n")
            elif c.get("deleted_file"):
                parts.append(f"--- a/{old_path}\n")
                parts.append("+++ /dev/null\n")
            else:
                parts.append(f"--- a/{old_path}\n")
                parts.append(f"+++ b/{new_path}\n")
            diff_text = c.get("diff", "") or ""
            if diff_text and not diff_text.endswith("\n"):
                diff_text += "\n"
            parts.append(diff_text)
        return "".join(parts)

    # ------------------------------------------------------------------
    # Review create / fetch / delete
    # ------------------------------------------------------------------

    def create_review(
        self, owner: str, repo: str, pr_number: int, review: ReviewBody
    ) -> dict[str, Any]:
        """Create a review on a GitLab MR.

        Body is posted as a single MR note. Each inline comment becomes a
        separate discussion with a text-position object. The returned
        ``id`` field is the body note's ID; per-comment IDs are listed
        under ``_comment_ids`` on the result for the poster.
        """
        pid = _project_id(owner, repo)
        result_id: int | None = None
        body_text = review.body or ""

        if body_text:
            note_url = f"/projects/{pid}/merge_requests/{pr_number}/notes"
            response = self._client.post(note_url, json={"body": body_text})
            response.raise_for_status()
            result_id = response.json().get("id")

        comment_ids: list[int] = []
        if review.comments:
            mr = self.get_pull_request(owner, repo, pr_number)
            base_sha = mr.get("_gitlab_base_sha", "")
            start_sha = mr.get("_gitlab_start_sha", base_sha)
            head_sha = mr.get("_gitlab_head_sha", "")
            for comment in review.comments:
                discussion = _build_gitlab_discussion(
                    comment, base_sha=base_sha, start_sha=start_sha, head_sha=head_sha
                )
                if discussion is None:
                    logger.warning(
                        "Dropping inline comment on %s:%s — missing MR refs",
                        comment.path,
                        comment.line,
                    )
                    continue
                disc_url = f"/projects/{pid}/merge_requests/{pr_number}/discussions"
                response = self._client.post(disc_url, json=discussion)
                response.raise_for_status()
                disc_data = response.json()
                # First note in the new discussion is the inline comment.
                notes = disc_data.get("notes", [])
                if notes:
                    comment_ids.append(notes[0].get("id"))

        # Fallback id if no body was sent: synthesize from the first inline note.
        if result_id is None and comment_ids:
            result_id = comment_ids[0]

        return {"id": result_id, "comment_ids": comment_ids}

    def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict[str, Any]]:
        """Return inline notes on the MR.

        GitLab has no "review object" grouping individual notes, so we
        return all inline-position notes on the MR. ``review_id`` is
        unused. Each entry has at least ``id`` so the poster can store it.
        """
        del review_id
        url = f"/projects/{_project_id(owner, repo)}/merge_requests/{pr_number}/notes"
        response = self._client.get(url, params={"per_page": 100})
        response.raise_for_status()
        notes: list[dict[str, Any]] = response.json()
        return [n for n in notes if n.get("position")]

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return general (non-inline) MR notes, mapped to GitHub's issue-comment shape."""
        url = f"/projects/{_project_id(owner, repo)}/merge_requests/{issue_number}/notes"
        params: dict[str, str | int] = {"per_page": 100}
        if since:
            # GitLab takes ``updated_after`` (ISO 8601), GitHub takes ``since``.
            params["updated_after"] = since
        response = self._client.get(url, params=params)
        response.raise_for_status()
        notes: list[dict[str, Any]] = response.json()
        return [_normalize_note(n) for n in notes if not n.get("position")]

    def delete_review_comment(self, owner: str, repo: str, pr_number: int, comment_id: int) -> None:
        url = f"/projects/{_project_id(owner, repo)}/merge_requests/{pr_number}/notes/{comment_id}"
        response = self._client.delete(url)
        response.raise_for_status()

    def get_authenticated_user(self) -> dict[str, Any]:
        """Fetch the authenticated user. GitLab returns ``username``; map to ``login``."""
        response = self._client.get("/user")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if "login" not in data and "username" in data:
            data["login"] = data["username"]
        return data

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Shape adapters
# ---------------------------------------------------------------------------


def _normalize_mr(mr: dict[str, Any]) -> dict[str, Any]:
    """Translate a GitLab MR JSON object into the GitHub-shaped dict the poller expects.

    Also stashes ``_gitlab_base_sha``, ``_gitlab_start_sha``, and
    ``_gitlab_head_sha`` on the returned dict — these are an internal
    carrier between ``get_pull_request`` and ``create_review`` (the
    discussion position object needs all three SHAs). External callers
    should treat them as private and not depend on them.
    """
    out = dict(mr)
    # Number / id (frank uses iid as the user-visible PR number).
    if "iid" in out and "number" not in out:
        out["number"] = out["iid"]
    # User shape: GitLab has author.username.
    author = out.get("author") or {}
    if isinstance(author, dict) and "login" not in author and "username" in author:
        author = dict(author)
        author["login"] = author["username"]
        out["user"] = author
    # html_url / web_url.
    if "html_url" not in out and "web_url" in out:
        out["html_url"] = out["web_url"]
    if "diff_url" not in out and out.get("html_url"):
        out["diff_url"] = out["html_url"] + ".diff"
    # Body / description.
    if "body" not in out and "description" in out:
        out["body"] = out.get("description") or ""
    # State mapping.
    state = out.get("state", "")
    if state == "opened":
        out["state"] = "open"
    elif state == "merged":
        out["state"] = "closed"
    # Labels: GitLab returns plain strings; GitHub returns [{name: "..."}].
    labels = out.get("labels") or []
    if labels and all(isinstance(label, str) for label in labels):
        out["labels"] = [{"name": label} for label in labels]
    # Reviewers: GitLab uses `reviewers` with username; GitHub uses
    # `requested_reviewers` with login.
    if "requested_reviewers" not in out:
        reviewers = out.get("reviewers") or []
        out["requested_reviewers"] = [
            {"login": r.get("username", "")} for r in reviewers if isinstance(r, dict)
        ]
    # Mergeable: GitLab uses ``merge_status`` ("can_be_merged" / "cannot_be_merged").
    if "mergeable" not in out:
        ms = out.get("merge_status")
        if ms == "can_be_merged":
            out["mergeable"] = True
        elif ms == "cannot_be_merged":
            out["mergeable"] = False
    # Base / head refs and SHAs come from `diff_refs`. Stash them on
    # underscored keys for the create_review path; also expose
    # GitHub-shaped base/head for the poller.
    diff_refs = out.get("diff_refs") or {}
    if isinstance(diff_refs, dict):
        out["_gitlab_base_sha"] = diff_refs.get("base_sha", "")
        out["_gitlab_start_sha"] = diff_refs.get("start_sha", "")
        out["_gitlab_head_sha"] = diff_refs.get("head_sha", "")
    if "base" not in out:
        out["base"] = {
            "ref": out.get("target_branch", ""),
            "sha": out.get("_gitlab_base_sha", ""),
        }
    if "head" not in out:
        out["head"] = {
            "ref": out.get("source_branch", ""),
            "sha": out.get("_gitlab_head_sha", ""),
        }
    return out


def _normalize_note(note: dict[str, Any]) -> dict[str, Any]:
    """Translate a GitLab note into a GitHub-shaped issue comment."""
    out = dict(note)
    author = out.get("author") or {}
    if isinstance(author, dict) and "login" not in author and "username" in author:
        author = dict(author)
        author["login"] = author["username"]
        out["user"] = author
    return out


def _build_gitlab_discussion(
    comment: ReviewComment, *, base_sha: str, start_sha: str, head_sha: str
) -> dict[str, Any] | None:
    """Build the JSON payload for ``POST /merge_requests/{iid}/discussions``.

    Returns ``None`` if SHAs are missing (we can't post an inline comment
    without a position).
    """
    if comment.line is None:
        # Plain note (file-level comments aren't supported as inline
        # discussions on GitLab without a line; promote to a general note).
        return {"body": comment.body}
    if not (base_sha and head_sha):
        return None

    position: dict[str, Any] = {
        "base_sha": base_sha,
        "start_sha": start_sha or base_sha,
        "head_sha": head_sha,
        "position_type": "text",
        "new_path": comment.path,
        "old_path": comment.path,
    }
    if comment.side == "LEFT":
        position["old_line"] = comment.line_end if comment.line_end else comment.line
    else:
        position["new_line"] = comment.line_end if comment.line_end else comment.line
    return {"body": comment.body, "position": position}
