"""
GitHub API client using httpx.

Implements the ``ForgeClient`` ABC. ``create_review`` accepts the
forge-agnostic ``ReviewBody`` dataclass and converts to GitHub's wire
format internally, so callers can target any forge uniformly.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx
from bs4 import BeautifulSoup

from franktheunicorn.backends.base import (
    MAX_LISTED_PULL_REQUESTS,
    ForgeClient,
    ReviewBody,
    ReviewComment,
    infer_username,
)
from franktheunicorn.data_access.base import GITHUB_API_BASE, GITHUB_WEB_BASE

if TYPE_CHECKING:
    from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

logger = logging.getLogger(__name__)


class GitHubClient(ForgeClient):
    """ForgeClient implementation backed by the GitHub REST API.

    Pass a ``GitHubRateLimiter`` to pace reads and track the remaining hourly
    quota. Ingestion runs hundreds of reads per cycle on a busy repo; without
    a limiter it happily burns the whole hourly budget, and every call after
    that comes back 403 and gets misread as a broken token.
    """

    # list_pull_requests paginates to MAX_LISTED_PULL_REQUESTS, so a PR missing
    # from the result is genuinely not open (unless the result hit that cap, or
    # came from the scrape fallback — the poller checks both).
    lists_all_open_pull_requests = True

    def __init__(
        self,
        token: str = "",
        base_url: str = GITHUB_API_BASE,
        rate_limiter: GitHubRateLimiter | None = None,
        *,
        pace_requests: bool = True,
    ) -> None:
        self._rate_limiter = rate_limiter
        # The limiter's brake is a blocking sleep — up to 30s when the quota is
        # spent, ~1s under bucket contention. That's correct for the worker and
        # wrong for a web request, where it would hang the operator's click.
        # pace_requests=False keeps the quota *tracking* (headers still feed the
        # shared limiter) and drops the waiting.
        self._pace_requests = pace_requests
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

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET with rate-limit pacing and quota tracking.

        Reads go through here; writes (create_review, delete) are low-volume
        and go direct.
        """
        if self._rate_limiter is not None and self._pace_requests:
            self._rate_limiter.acquire()
        response = self._client.get(url, **kwargs)
        if self._rate_limiter is not None:
            self._rate_limiter.update_from_headers(response.headers)
        return response

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
        # ingestion. Capped at MAX_LISTED_PULL_REQUESTS per cycle.
        result: list[dict[str, Any]] = []
        for page in range(1, (MAX_LISTED_PULL_REQUESTS // 100) + 1):
            response = self._get(url, params={"state": state, "per_page": 100, "page": page})
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
        response = self._get(url)
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
            response = self._get(url, params={"per_page": 100, "page": page})
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            result.extend(data)
            if len(data) < 100:
                break
        return result

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Fetch the diff for a PR."""
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        response = self._get(url, headers={"Accept": "application/vnd.github.v3.diff"})
        response.raise_for_status()
        return response.text

    def get_commit_diff(self, owner: str, repo: str, sha: str) -> str:
        """Fetch the unified diff for a single commit.

        Uses ``GET /repos/{owner}/{repo}/commits/{sha}`` with the diff media
        type. Consumed by the backport check for cherry-pick-of-<sha> refs.
        """
        url = f"/repos/{owner}/{repo}/commits/{sha}"
        response = self._get(url, headers={"Accept": "application/vnd.github.v3.diff"})
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
        response = self._get(url, params={"per_page": 100})
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
        response = self._get(url, params=params)
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
        truncated at 100. Network/API errors are allowed to propagate so
        callers can distinguish an unavailable source from a genuine empty
        response.
        """
        url = f"/repos/{owner}/{repo}/contributors"
        all_logins: list[str] = []
        for page in range(1, 6):
            response = self._get(url, params={"per_page": 100, "page": page, "anon": "false"})
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            if not data:
                break
            all_logins.extend(entry["login"] for entry in data if entry.get("login"))
        return all_logins

    def get_authenticated_user(self) -> dict[str, Any]:
        """Fetch the authenticated user's profile (GET /user)."""
        response = self._get("/user")
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
        return self._search_prs(
            f"involves:{username} type:pr state:open", max_results, what="mention scan"
        )

    def search_prs_authored_by(self, username: str, max_results: int = 100) -> list[dict[str, Any]]:
        """Search for open PRs authored by ``username``.

        ``author:`` rather than relying on ``involves:`` to cover it — see the
        base-class docstring for why the operator's own PRs get their own call.
        """
        return self._search_prs(
            f"author:{username} type:pr state:open", max_results, what="own-PR scan"
        )

    #: Search results per page. GitHub's own ceiling for /search/issues.
    _SEARCH_PAGE = 100

    def _search_prs(self, query: str, max_results: int, *, what: str) -> list[dict[str, Any]]:
        """Run one issue search, paginated, sorted most-recently-updated first.

        Two things the single-page version got wrong. It passed ``per_page`` =
        ``max_results``, which GitHub silently clamps to 100, so any caller asking
        for more quietly got 100 — and it took GitHub's default *relevance* sort,
        which for "PRs involving me" is not a defensible order to truncate on.
        Sorting by ``updated`` desc means a truncated result set is the N most
        recently active, which is the set worth having.
        """
        items: list[dict[str, Any]] = []
        try:
            for page in range(1, (max_results // self._SEARCH_PAGE) + 2):
                response = self._get(
                    "/search/issues",
                    params={
                        "q": query,
                        "per_page": min(self._SEARCH_PAGE, max_results - len(items)),
                        "page": page,
                        "sort": "updated",
                        "order": "desc",
                    },
                )
                if response.status_code in (403, 422, 429):
                    logger.info(
                        "GitHub search rate-limited or unavailable (status %d); "
                        "%s returning %d result(s) so far.",
                        response.status_code,
                        what,
                        len(items),
                    )
                    return items
                response.raise_for_status()
                data: dict[str, Any] = response.json()
                page_items: list[dict[str, Any]] = data.get("items", [])
                items.extend(page_items)
                if len(page_items) < self._SEARCH_PAGE or len(items) >= max_results:
                    break
        except Exception:
            logger.debug("GitHub %s failed for query %r", what, query, exc_info=True)
            # Whatever came back before the failure is still worth having: a
            # second-page timeout shouldn't discard the first page.
            return items
        return items[:max_results]

    def close(self) -> None:
        self._client.close()


_REQUIRED_SCOPES = {"repo", "public_repo"}
_FINE_GRAINED_NOTE = "Fine-grained PAT: enable 'Pull requests: Read' under repository permissions."


def _is_rate_limit_response(response: httpx.Response) -> bool:
    """True when a 403/429 is GitHub throttling us rather than refusing us.

    Covers both flavours. The primary limit zeroes x-ratelimit-remaining. A
    *secondary* limit leaves the quota headers intact and non-zero, saying so
    only in the body and in retry-after — so the remaining header can't be
    treated as the last word or every secondary throttle gets misreported as a
    broken token.
    """
    if response.status_code not in (403, 429):
        return False

    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining is not None:
        with contextlib.suppress(ValueError):
            if int(remaining) <= 0:
                return True

    # The one header GitHub documents for secondary limits.
    if response.headers.get("retry-after"):
        return True

    with contextlib.suppress(Exception):
        return "rate limit" in str(response.json().get("message", "")).lower()
    return False


def _log_auth_suggestions(owner: str, repo: str, response: httpx.Response | None = None) -> None:
    """Log actionable suggestions when the GitHub API returns 401 or 403."""
    status = response.status_code if response is not None else 401

    # A 403 with no quota left is a rate limit, not an auth problem. Saying
    # "check your token scopes" here sends the operator chasing the wrong bug.
    if response is not None and _is_rate_limit_response(response):
        retry_after = response.headers.get("retry-after", "")
        when = (
            f"retry after {retry_after}s"
            if retry_after
            else f"resets at epoch {response.headers.get('x-ratelimit-reset') or 'unknown'}"
        )
        logger.error(
            "GitHub is throttling us on %s/%s (limit %s/hour, %s). This is a rate "
            "limit, not an auth failure — the token is fine. Reduce poll frequency, "
            "raise poll_refresh_hours, or trim the number of polled projects.",
            owner,
            repo,
            response.headers.get("x-ratelimit-limit", "?"),
            when,
        )
        return

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
        frank_token_set = bool(os.environ.get("FRANK_GITHUB_TOKEN"))
        generic_token_set = bool(os.environ.get("GITHUB_TOKEN"))
        # These checks assume the common case: this forge entry's token came
        # from FRANK_GITHUB_TOKEN, the default operator.yaml sets
        # `github_token: "${FRANK_GITHUB_TOKEN}"`. A forge entry can instead
        # point `token:` at any other env var (multi-account setups), in
        # which case these two vars say nothing about what was actually
        # sent — hence the hedge appended below.
        if not frank_token_set and generic_token_set:
            token_hint = (
                "FRANK_GITHUB_TOKEN is not set, but GITHUB_TOKEN is — franktheunicorn "
                "only reads FRANK_GITHUB_TOKEN by default (see .env.example). GITHUB_TOKEN "
                "is ignored unless a forge entry explicitly points `token:` at it; rename "
                "it or set FRANK_GITHUB_TOKEN as well."
            )
        elif not frank_token_set:
            token_hint = (
                "FRANK_GITHUB_TOKEN is not set or is empty — check your .env file. If this "
                "repo's forge entry in operator.yaml uses a different `token:` variable, "
                "check that one instead."
            )
        else:
            token_hint = (
                "FRANK_GITHUB_TOKEN is set but was rejected by GitHub — see causes below. "
                "(If this repo's forge entry uses a different `token:` variable, the "
                "rejected token may not be FRANK_GITHUB_TOKEN's value.)"
            )
        logger.error(
            "GitHub API returned 401 Unauthorized for %s/%s. Possible causes:\n"
            "  1. %s\n"
            "  2. Token has expired or been revoked — generate a new one at "
            "https://github.com/settings/tokens\n"
            "  3. Classic PAT missing 'public_repo' (public) or 'repo' (private) scope.%s\n"
            "  4. Token is for a different GitHub account that cannot access %s/%s.\n"
            "  5. %s",
            owner,
            repo,
            token_hint,
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
