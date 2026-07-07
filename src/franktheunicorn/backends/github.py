"""
GitHub API client using httpx.

Implements the ``ForgeClient`` ABC. ``create_review`` accepts the
forge-agnostic ``ReviewBody`` dataclass and converts to GitHub's wire
format internally, so callers can target any forge uniformly.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import httpx
from bs4 import BeautifulSoup

from franktheunicorn.backends.base import ForgeClient, ReviewBody, ReviewComment, infer_username
from franktheunicorn.data_access.base import GITHUB_API_BASE, GITHUB_WEB_BASE

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
            logger.debug(
                "GitHub token loaded: %s...%s (%d chars)",
                token[:2],
                token[-2:],
                len(token),
            )
        else:
            logger.debug("GitHub client created with no token (unauthenticated)")
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=30.0,
        )

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        """Fetch open pull requests for a repository.

        Falls back to HTML scraping when the API returns 401, and logs
        actionable suggestions to help the operator fix their token.
        """
        url = f"/repos/{owner}/{repo}/pulls"
        # Paginate: spark-scale repos have hundreds of open PRs; a single
        # 50-item page silently hid everything but the newest PRs from
        # ingestion. Capped at 10 pages (1000 PRs) per cycle.
        result: list[dict[str, Any]] = []
        for page in range(1, 11):
            response = self._client.get(url, params={"state": state, "per_page": 100, "page": page})
            if response.status_code in (401, 403):
                _log_auth_suggestions(owner, repo, response)
                logger.info(
                    "Falling back to HTML scrape for %s/%s PR listing (API returned %d)",
                    owner,
                    repo,
                    response.status_code,
                )
                return _list_pull_requests_via_scrape(owner, repo, state=state)
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            result.extend(data)
            if len(data) < 100:
                break
        return result

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch a single PR detail (includes mergeable status)."""
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        response = self._client.get(url)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Fetch the list of files changed in a PR.

        Paginates (up to 10 pages / 1000 files) — a single page truncated
        large PRs at 100 files, corrupting path-overlap/test-detection
        signals computed from ``changed_files``.
        """
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
        result: list[dict[str, Any]] = []
        for page in range(1, 11):
            response = self._client.get(url, params={"per_page": 100, "page": page})
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            result.extend(data)
            if len(data) < 100:
                break
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
        format and populates ``comment_ids_by_key`` on the result by querying
        the review's comments after creation. GitHub returns review
        comments in posting order, so the IDs align with ``review.comments``.
        """
        # GitHub rejects review comments that carry neither line nor position
        # (422, failing the whole batch). Fold file-level comments (no line
        # number, e.g. CodeRabbit summaries) into the review body instead.
        inline_comments = [c for c in review.comments if c.line is not None]
        file_level = [c for c in review.comments if c.line is None]

        body_text = review.body or ""
        if file_level:
            extras = "\n\n".join(
                f"**{c.path}**: {c.body}" if c.path else c.body for c in file_level
            )
            body_text = f"{body_text}\n\n{extras}".strip() if body_text else extras

        payload: dict[str, Any] = {"event": review.event}
        if body_text:
            payload["body"] = body_text
        payload["comments"] = [_to_github_comment(c) for c in inline_comments]

        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        result: dict[str, Any] = response.json()

        comment_ids_by_key: dict[str, int] = {}
        review_id = result.get("id")
        if review_id and inline_comments:
            try:
                posted_comments = self.get_review_comments(owner, repo, pr_number, review_id)
                fetched_ids = [c["id"] for c in posted_comments if "id" in c]
                for i, fid in enumerate(fetched_ids):
                    if i < len(inline_comments):
                        key = inline_comments[i].correlation_key
                        if key:
                            comment_ids_by_key[key] = fid
            except Exception:
                logger.warning(
                    "Could not fetch posted comment IDs for %s/%s#%d review %d",
                    owner,
                    repo,
                    pr_number,
                    review_id,
                )
        result["comment_ids_by_key"] = comment_ids_by_key
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

    def list_contributors(self, owner: str, repo: str) -> list[str]:
        """Fetch contributor logins from the GitHub contributors API.

        Paginates up to 5 pages (500 contributors) so large repos aren't
        truncated at 100. On any error returns an empty list so the caller
        can fall back to DB-only known-author detection.
        """
        url = f"/repos/{owner}/{repo}/contributors"
        try:
            all_logins: list[str] = []
            for page in range(1, 6):
                response = self._client.get(
                    url, params={"per_page": 100, "page": page, "anon": "false"}
                )
                response.raise_for_status()
                data: list[dict[str, Any]] = response.json()
                if not data:
                    break
                all_logins.extend(entry["login"] for entry in data if entry.get("login"))
            return all_logins
        except Exception:
            logger.debug("Could not fetch contributors for %s/%s", owner, repo, exc_info=True)
            return []

    def get_authenticated_user(self) -> dict[str, Any]:
        """Fetch the authenticated user's profile (GET /user)."""
        response = self._client.get("/user")
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def search_prs_involving(self, username: str, max_results: int = 100) -> list[dict[str, Any]]:
        """Search for open PRs where ``username`` is mentioned, assigned, or requested as reviewer.

        Uses GitHub search query: ``involves:{username} type:pr state:open``.
        The ``involves:`` qualifier matches @mentions, assignments, and review requests.
        Returns raw search-API items; each has a ``pull_request`` key and ``repository_url``.
        Returns [] gracefully on rate-limit (403/422/429) or any other failure.
        """
        url = "/search/issues"
        query = f"involves:{username} type:pr state:open"
        try:
            response = self._client.get(url, params={"q": query, "per_page": max_results})
            if response.status_code in (403, 422, 429):
                logger.info(
                    "GitHub search rate-limited or unavailable (status %d); skipping mention scan.",
                    response.status_code,
                )
                return []
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            items: list[dict[str, Any]] = data.get("items", [])
            return items
        except Exception:
            logger.debug("PR mention scan failed for %s", username, exc_info=True)
            return []

    def close(self) -> None:
        self._client.close()


_REQUIRED_SCOPES = {"repo", "public_repo"}
_FINE_GRAINED_NOTE = "Fine-grained PAT: enable 'Pull requests: Read' under repository permissions."


def _log_auth_suggestions(owner: str, repo: str, response: httpx.Response | None = None) -> None:
    """Log actionable suggestions when the GitHub API returns 401 or 403."""
    status = response.status_code if response is not None else 401

    # Parse granted scopes from the response header when available.
    granted: set[str] = set()
    missing_scope_hint = ""
    if response is not None:
        raw_scopes = response.headers.get("X-OAuth-Scopes", "")
        if raw_scopes:
            granted = {s.strip() for s in raw_scopes.split(",") if s.strip()}
            if not granted & _REQUIRED_SCOPES:
                missing_scope_hint = (
                    f"\n  -> Your token has scopes: {raw_scopes or '(none)'}. "
                    f"Add 'public_repo' (public repos) or 'repo' (private repos)."
                )

    if status == 403:
        logger.error(
            "GitHub API returned 403 Forbidden for %s/%s. Possible causes:\n"
            "  1. Classic PAT: token is valid but lacks required scope.%s\n"
            "  2. %s\n"
            "  3. Organization SSO: token needs SSO authorization at "
            "https://github.com/settings/tokens\n"
            "  4. Repository is private and token only has 'public_repo' scope.",
            owner,
            repo,
            missing_scope_hint,
            _FINE_GRAINED_NOTE,
        )
    else:
        logger.error(
            "GitHub API returned 401 Unauthorized for %s/%s. Possible causes:\n"
            "  1. GITHUB_TOKEN is not set or is empty — check your .env file.\n"
            "  2. Token has expired or been revoked — generate a new one at "
            "https://github.com/settings/tokens\n"
            "  3. Classic PAT missing 'public_repo' (public) or 'repo' (private) scope.%s\n"
            "  4. Token is for a different GitHub account that cannot access %s/%s.\n"
            "  5. %s",
            owner,
            repo,
            missing_scope_hint,
            owner,
            repo,
            _FINE_GRAINED_NOTE,
        )


def _list_pull_requests_via_scrape(
    owner: str, repo: str, state: str = "open"
) -> list[dict[str, Any]]:
    """Scrape the GitHub issues search page as a fallback when the API is unavailable.

    Returns a list of minimal PR dicts with the same keys that poller.py reads:
    number, title, user.login, state, html_url, diff_url, labels,
    requested_reviewers, assignees, draft, additions, deletions.
    Missing numeric fields default to 0; missing lists default to [].
    """
    # GitHub now redirects /pulls to /issues?q=is:open+is:pr; go there directly.
    url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/issues"
    params: dict[str, str] = {"q": "is:open is:pr" if state == "open" else f"is:{state} is:pr"}
    try:
        with httpx.Client(
            headers={"User-Agent": "franktheunicorn/scrape-fallback"}, timeout=30.0
        ) as scrape_client:
            response = scrape_client.get(url, params=params, follow_redirects=True)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Scrape fallback also failed for %s/%s: %s", owner, repo, exc)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, Any]] = []

    # Current GitHub DOM: each PR row has exactly one anchor with
    # data-testid="issue-pr-title-link" whose href contains /pull/<number>.
    title_links = soup.select("a[data-testid='issue-pr-title-link']")
    for title_el in title_links:
        href = title_el.get("href", "")
        if not isinstance(href, str) or "/pull/" not in href:
            continue

        pr_number = 0
        with contextlib.suppress(ValueError, IndexError):
            pr_number = int(href.rstrip("/").split("/")[-1])
        if pr_number == 0:
            continue

        title = title_el.get_text(strip=True) or f"PR #{pr_number}"

        # Author link: walk up the DOM until we find an ancestor that contains
        # the author%3A link (appears in the same row container).
        author = ""
        node = title_el.parent
        for _ in range(12):
            if node is None:
                break
            author_el = node.select_one("a[href*='author%3A']")
            if author_el:
                author = author_el.get_text(strip=True)
                break
            node = node.parent

        pr_url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/pull/{pr_number}"
        results.append(
            {
                # ``_scraped`` marks this as a degraded (HTML-scrape) record:
                # only number/title/author/state/url are real; body, labels,
                # additions, timestamps etc. are placeholders. The poller must
                # not overwrite good DB values with these on an existing row.
                "_scraped": True,
                "number": pr_number,
                "id": 0,
                "title": title,
                "user": {"login": author},
                "state": state,
                "html_url": pr_url,
                "diff_url": f"{pr_url}.diff",
                "body": "",
                "labels": [],
                "requested_reviewers": [],
                "assignees": [],
                "draft": False,
                "additions": 0,
                "deletions": 0,
                "created_at": "",
                "updated_at": "",
            }
        )

    if not results:
        logger.warning(
            "Scrape fallback for %s/%s returned 0 PRs — GitHub HTML structure may have changed",
            owner,
            repo,
        )
    else:
        logger.info("Scrape fallback for %s/%s found %d PR(s)", owner, repo, len(results))
    return results


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
