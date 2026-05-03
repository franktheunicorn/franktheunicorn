"""
Gerrit Code Review API client.

Maps the forge-agnostic ``ForgeClient`` interface onto Gerrit's REST API.
Gerrit's terminology differs from a pull-request forge:

- A "change" is the unit of review (analogous to a PR/MR). Each change has
  a numeric ``_number`` (used as Frank's ``pr_number``) and a stable
  ``change_id`` hash (``Iabc...``).
- Each change has one or more "revisions" (patchsets). Frank always
  reviews the current revision.
- A "review" is a JSON payload posted to
  ``/changes/{id}/revisions/{rev}/review`` containing an optional
  message, optional inline comments grouped by file path, and optional
  vote labels.
- "Project" combines what GitHub splits into ``owner/repo``. Frank joins
  ``{owner}/{repo}`` (or just ``{repo}`` when ``owner`` is empty) and
  URL-encodes the result.

Two Gerrit-specific wire-protocol quirks the client handles:

- Authenticated endpoints live under ``/a/``; anonymous endpoints don't.
- Every JSON response is prefixed with ``)]}'`` (an XSSI guard) which
  must be stripped before parsing.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

from franktheunicorn.backends.base import ForgeClient, ReviewBody, ReviewComment

logger = logging.getLogger(__name__)


_XSSI_PREFIX = ")]}'"


def _decode_gerrit_json(payload: bytes | str) -> Any:
    """Strip the Gerrit XSSI prefix and parse the remaining JSON.

    Gerrit prepends ``)]}'`` to every JSON response (including a trailing
    newline) so that responses can't be eval'd by a browser. Tolerate
    whitespace around the prefix; raise ``ValueError`` if the body isn't
    valid JSON afterwards.
    """
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    text = text.lstrip()
    if text.startswith(_XSSI_PREFIX):
        text = text[len(_XSSI_PREFIX) :]
    return json.loads(text)


def _project_name(owner: str, repo: str) -> str:
    """URL-encode the Gerrit project name.

    Gerrit projects are flat strings that may contain ``/`` (e.g.
    ``chromium/src``). Frank exposes ``owner/repo``; when ``owner`` is
    empty we treat ``repo`` as the full project name.
    """
    name = f"{owner}/{repo}" if owner else repo
    return quote(name, safe="")


_FILE_STATUS_MAP: dict[str, str] = {
    "A": "added",
    "D": "removed",
    "R": "renamed",
    "C": "copied",
    "W": "rewritten",
    "M": "modified",
}


class GerritClient(ForgeClient):
    """ForgeClient backed by the Gerrit Code Review REST API.

    ``token`` may be the HTTP password alone (in which case ``username``
    must be provided), or a combined ``user:password`` string. Anonymous
    use is supported when neither is set, but most of Frank's actions
    require authentication.
    """

    def __init__(self, token: str = "", base_url: str = "", username: str = "") -> None:
        if not base_url:
            msg = "GerritClient requires base_url (e.g. https://review.example.com)"
            raise ValueError(msg)

        auth: tuple[str, str] | None = None
        if token:
            if username:
                auth = (username, token)
            elif ":" in token:
                user_part, _, pass_part = token.partition(":")
                if user_part and pass_part:
                    auth = (user_part, pass_part)
        self._authed = auth is not None

        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Accept": "application/json"},
            timeout=30.0,
            auth=auth,
        )

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _prefix(self) -> str:
        """Authenticated requests must be prefixed with ``/a``."""
        return "/a" if self._authed else ""

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(f"{self._prefix()}{path}", params=params)
        response.raise_for_status()
        return _decode_gerrit_json(response.content)

    def _post_json(self, path: str, body: Any) -> Any:
        response = self._client.post(f"{self._prefix()}{path}", json=body)
        response.raise_for_status()
        if not response.content:
            return None
        return _decode_gerrit_json(response.content)

    def _resolve_change(self, pr_number: int) -> dict[str, Any]:
        """Look up the change and its current revision SHA by numeric ID.

        Returns a normalized dict with ``id`` (numeric, as a string —
        Gerrit's preferred URL form), ``change_id`` (the ``I...`` hash),
        and ``current_revision``. Raises ``LookupError`` if the change
        isn't found.
        """
        result = self._get_json(
            "/changes/",
            params={"q": str(pr_number), "o": ["CURRENT_REVISION"]},
        )
        if not result:
            msg = f"Gerrit change {pr_number} not found"
            raise LookupError(msg)
        change = result[0]
        return {
            "id": str(change.get("_number", pr_number)),
            "change_id": change.get("change_id", ""),
            "current_revision": change.get("current_revision", "current"),
        }

    # ------------------------------------------------------------------
    # Change discovery
    # ------------------------------------------------------------------

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        gerrit_status = {"open": "open", "closed": "closed", "merged": "merged"}.get(state, state)
        # Gerrit's query is space-separated; httpx will URL-encode the spaces.
        project = f"{owner}/{repo}" if owner else repo
        query = f"project:{project} status:{gerrit_status}"
        result = self._get_json(
            "/changes/",
            params={
                "q": query,
                "n": 50,
                "o": ["CURRENT_REVISION", "DETAILED_ACCOUNTS"],
            },
        )
        return [self._normalize_change(c, owner, repo) for c in result]

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        result = self._get_json(
            "/changes/",
            params={
                "q": str(pr_number),
                "o": [
                    "CURRENT_REVISION",
                    "DETAILED_ACCOUNTS",
                    "DETAILED_LABELS",
                    "CURRENT_COMMIT",
                ],
            },
        )
        if not result:
            msg = f"Gerrit change {pr_number} not found"
            raise LookupError(msg)
        return self._normalize_change(result[0], owner, repo)

    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        change = self._resolve_change(pr_number)
        files: dict[str, dict[str, Any]] = self._get_json(
            f"/changes/{change['id']}/revisions/{change['current_revision']}/files/"
        )
        out: list[dict[str, Any]] = []
        for path, info in files.items():
            if path == "/COMMIT_MSG":
                # Gerrit synthesizes this entry for the commit message; not
                # a real file change.
                continue
            status_letter = info.get("status", "M") if isinstance(info, dict) else "M"
            out.append(
                {"filename": path, "status": _FILE_STATUS_MAP.get(status_letter, "modified")}
            )
        return out

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return the change's current revision as a unified diff.

        Gerrit's ``/patch`` endpoint returns the full mbox-formatted
        commit (including headers and the diff body) base64-encoded. We
        decode and return it as-is — callers that only need the diff hunks
        already tolerate leading commit metadata.
        """
        change = self._resolve_change(pr_number)
        url = (
            f"{self._prefix()}/changes/{change['id']}/revisions/{change['current_revision']}/patch"
        )
        response = self._client.get(url)
        response.raise_for_status()
        text = response.text.strip()
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            # Some Gerrit deployments return raw text when ?download=1 is
            # implied; fall back to the body as-is.
            return response.text

    # ------------------------------------------------------------------
    # Review create / fetch / delete
    # ------------------------------------------------------------------

    def create_review(
        self, owner: str, repo: str, pr_number: int, review: ReviewBody
    ) -> dict[str, Any]:
        """Submit a review against the change's current revision.

        Inline comments are grouped by file path into Gerrit's
        ``comments`` map. The ``event`` field of ``ReviewBody`` is not
        translated to a Code-Review label here — Frank submits review
        comments without voting in v1; operators can vote separately if
        they choose.
        """
        del owner, repo  # Gerrit changes are uniquely identified by number.
        change = self._resolve_change(pr_number)
        cid = change["id"]
        rev = change["current_revision"]

        comments_by_file: dict[str, list[dict[str, Any]]] = {}
        keys_by_file: dict[str, list[str]] = {}
        for comment in review.comments:
            wire = _to_gerrit_comment(comment)
            comments_by_file.setdefault(comment.path, []).append(wire)
            keys_by_file.setdefault(comment.path, []).append(comment.correlation_key)

        payload: dict[str, Any] = {}
        if review.body:
            payload["message"] = review.body
        if comments_by_file:
            payload["comments"] = comments_by_file

        result = self._post_json(f"/changes/{cid}/revisions/{rev}/review", payload)

        comment_ids_by_key: dict[str, int | str] = {}
        if comments_by_file:
            try:
                posted = self._get_json(f"/changes/{cid}/comments")
                for path, wires in comments_by_file.items():
                    file_comments = posted.get(path, []) if isinstance(posted, dict) else []
                    # Gerrit appends new comments to the per-file list. Walk
                    # the tail to recover IDs in the same order they were
                    # sent.
                    tail = file_comments[-len(wires) :] if file_comments else []
                    keys = keys_by_file.get(path, [])
                    for idx, key in enumerate(keys):
                        if idx >= len(tail) or not key:
                            continue
                        cid_val = tail[idx].get("id")
                        if cid_val is not None:
                            comment_ids_by_key[key] = cid_val
            except Exception:
                logger.warning(
                    "Could not fetch posted Gerrit comment IDs for change %s", cid, exc_info=True
                )

        out: dict[str, Any] = dict(result) if isinstance(result, dict) else {}
        # Gerrit's review response has no top-level id; use the change
        # number so the poster has something stable to record.
        out.setdefault("id", pr_number)
        out["comment_ids_by_key"] = comment_ids_by_key
        return out

    def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict[str, Any]]:
        """Return all inline comments on the change.

        Gerrit has no first-class "review" object aggregating individual
        comments, so ``review_id`` is unused. Each returned dict carries
        the comment fields plus a synthesized ``path`` so callers can tell
        which file it belongs to.
        """
        del owner, repo, review_id
        change = self._resolve_change(pr_number)
        result = self._get_json(f"/changes/{change['id']}/comments")
        out: list[dict[str, Any]] = []
        if not isinstance(result, dict):
            return out
        for path, comments in result.items():
            for c in comments:
                entry = dict(c)
                entry["path"] = path
                out.append(entry)
        return out

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return change-level messages, mapped to GitHub's issue-comment shape.

        Gerrit's ``/messages`` endpoint returns top-level chatter on the
        change (no ``since`` parameter); we filter client-side when needed.
        """
        del owner, repo
        change = self._resolve_change(issue_number)
        result = self._get_json(f"/changes/{change['id']}/messages")
        out: list[dict[str, Any]] = []
        for msg in result or []:
            entry = dict(msg)
            entry.setdefault("body", entry.get("message", ""))
            entry.setdefault("created_at", entry.get("date", ""))
            author = entry.get("author")
            if isinstance(author, dict):
                author_out = dict(author)
                author_out.setdefault("login", author.get("username") or author.get("name") or "")
                entry["user"] = author_out
            if since and entry.get("date", "") < since:
                continue
            out.append(entry)
        return out

    def delete_review_comment(self, owner: str, repo: str, pr_number: int, comment_id: int) -> None:
        """Delete a posted inline comment (best-effort; admin-only on Gerrit).

        Gerrit requires a non-empty ``reason`` and exposes deletion as a
        POST under the comment's revision. Most operators won't have
        permission; the caller should treat failure as recoverable.
        """
        del owner, repo
        change = self._resolve_change(pr_number)
        url = (
            f"{self._prefix()}/changes/{change['id']}/revisions/"
            f"{change['current_revision']}/comments/{comment_id}/delete"
        )
        response = self._client.post(url, json={"reason": "Recalled by frank"})
        response.raise_for_status()

    def get_authenticated_user(self) -> dict[str, Any]:
        """Fetch the authenticated account; map ``username`` to ``login``."""
        result = self._get_json("/accounts/self")
        out: dict[str, Any] = dict(result) if isinstance(result, dict) else {}
        if "login" not in out:
            out["login"] = out.get("username") or out.get("name", "") or ""
        return out

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize_change(self, change: dict[str, Any], owner: str, repo: str) -> dict[str, Any]:
        """Translate a Gerrit change object into a GitHub-shaped PR dict.

        Maps the fields the poller depends on: ``number``, ``state``,
        ``user``, ``html_url``, ``title``, ``body``, ``base``/``head``,
        ``labels``, ``requested_reviewers``.
        """
        out = dict(change)
        if "number" not in out and "_number" in out:
            out["number"] = out["_number"]

        owner_field = out.get("owner") or {}
        if isinstance(owner_field, dict):
            user = dict(owner_field)
            user.setdefault("login", user.get("username") or user.get("name") or "")
            out["user"] = user

        # State mapping: Gerrit uses NEW/MERGED/ABANDONED.
        status = out.get("status", "")
        if status == "NEW":
            out["state"] = "open"
        elif status in ("MERGED", "ABANDONED"):
            out["state"] = "closed"

        out.setdefault("title", out.get("subject", ""))
        out.setdefault("body", "")

        project = f"{owner}/{repo}" if owner else repo
        if "html_url" not in out:
            number = out.get("number") or out.get("_number")
            if number is not None:
                out["html_url"] = f"{self._base_url}/c/{quote(project, safe='/')}/+/{number}"
        if "diff_url" not in out and out.get("html_url"):
            out["diff_url"] = out["html_url"]

        # base/head approximation: branch + current revision SHA.
        out["base"] = {"ref": out.get("branch", ""), "sha": ""}
        out["head"] = {"ref": "", "sha": out.get("current_revision", "")}

        # Labels: Gerrit's ``labels`` is a dict (Code-Review/Verified/etc.) —
        # not the GitHub free-form list. Surface the names without their
        # vote details to match the poller's expectations.
        labels = out.get("labels")
        if isinstance(labels, dict):
            out["labels"] = [{"name": name} for name in labels]
        elif labels is None:
            out["labels"] = []

        reviewers_field = out.get("reviewers") or {}
        flat_reviewers: list[dict[str, Any]] = []
        if isinstance(reviewers_field, dict):
            for bucket in ("REVIEWER", "CC"):
                for r in reviewers_field.get(bucket, []) or []:
                    if isinstance(r, dict):
                        flat_reviewers.append({"login": r.get("username") or r.get("name") or ""})
        out.setdefault("requested_reviewers", flat_reviewers)
        out.setdefault("mergeable", out.get("mergeable"))
        return out


def _to_gerrit_comment(comment: ReviewComment) -> dict[str, Any]:
    """Convert a normalized ReviewComment to Gerrit's CommentInput wire format.

    Gerrit groups comments by file at the request level (so ``path`` is
    not part of the per-comment payload). A multi-line comment uses the
    ``range`` field; a single-line comment uses ``line``; a comment with
    no line is a file-level comment.
    """
    wire: dict[str, Any] = {"message": comment.body}
    if comment.line_end is not None and comment.line_end > (comment.line or 0):
        wire["range"] = {
            "start_line": comment.line if comment.line is not None else comment.line_end,
            "start_character": 0,
            "end_line": comment.line_end,
            "end_character": 0,
        }
    elif comment.line is not None:
        wire["line"] = comment.line
    if comment.side == "LEFT":
        wire["side"] = "PARENT"
    return wire
