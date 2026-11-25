"""
GitHub API client using httpx.

Implements the ``ForgeClient`` ABC. ``create_review`` accepts the
forge-agnostic ``ReviewBody`` dataclass and converts to GitHub's wire
format internally, so callers can target any forge uniformly.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from bs4 import BeautifulSoup

from franktheunicorn.backends.base import (
    MAX_LISTED_PULL_REQUESTS,
    ForgeClient,
    NotificationPage,
    NotificationsUnavailableError,
    ReviewBody,
    ReviewComment,
    UpdatedPullRequests,
    infer_username,
)
from franktheunicorn.backends.conditional_cache import CachedReply, ConditionalCache, cache_key
from franktheunicorn.data_access.base import GITHUB_API_BASE, GITHUB_WEB_BASE

if TYPE_CHECKING:
    from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

logger = logging.getLogger(__name__)

#: Actions job logs fetched per ``get_commit_check_outputs`` /
#: ``get_commit_check_failures`` call (one request each).
_MAX_JOB_LOGS = 8

#: Failed jobs reported per linked workflow run (``_failed_run_jobs``).
_MAX_FAILED_JOBS = 12

#: ``https://github.com/<owner>/<repo>/actions/runs/<id>`` — a workflow run's page.
_ACTIONS_RUN_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/actions/runs/(\d+)")


def _linked_run(run: dict[str, Any], owner: str, repo: str) -> tuple[str, str, int] | None:
    """The workflow run *in another repository* that a check run stands for.

    Apache Spark runs a PR's tests in the contributor's fork and mirrors the
    result onto the PR as one ``Build`` check, with the fork run named in the
    check's ``output.text`` as ``{"owner", "repo", "run_id"}`` and linked from
    ``details_url``. A check whose run is in *this* repository is not linked:
    its own id is already the job id.
    """
    out = run.get("output")
    text = out.get("text") if isinstance(out, dict) else None
    if isinstance(text, str) and text.lstrip().startswith("{"):
        try:
            ref = json.loads(text)
        except ValueError:
            ref = None
        if isinstance(ref, dict):
            run_id = ref.get("run_id")
            ref_owner, ref_repo = ref.get("owner"), ref.get("repo")
            if (
                isinstance(ref_owner, str)
                and isinstance(ref_repo, str)
                and isinstance(run_id, int | str)
                and str(run_id).isdigit()
            ):
                return ref_owner, ref_repo, int(run_id)
    match = _ACTIONS_RUN_URL.search(str(run.get("details_url") or ""))
    if match is None:
        return None
    if (match.group(1).lower(), match.group(2).lower()) == (owner.lower(), repo.lower()):
        return None
    return match.group(1), match.group(2), int(match.group(3))


def _first_failed_step(steps: object) -> str:
    """The first failed step's name from an Actions job's ``steps``, or ``""``."""
    if not isinstance(steps, list):
        return ""
    for wanted in (("failure",), ("timed_out", "cancelled")):
        for step in steps:
            if isinstance(step, dict) and step.get("conclusion") in wanted:
                return str(step.get("name") or "")
    return ""


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
        self._conditional = ConditionalCache()
        #: Whether requests carry a token. A 304 is free only for an authenticated
        #: request, so a watcher polling every minute without one would spend the
        #: whole 60-an-hour unauthenticated quota on a single project.
        self.authenticated = bool(token)

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET with rate-limit pacing, quota tracking and conditional requests.

        Reads go through here; writes (create_review, delete) are low-volume
        and go direct.

        Every read sends back the ``ETag`` of the last ``200`` for the same URL,
        query and media type. GitHub answers ``304`` when nothing changed, and a
        304 does not count against the primary rate limit; the caller gets the
        cached body as an ordinary ``200`` and cannot tell the difference.
        """
        if self._rate_limiter is not None and self._pace_requests:
            self._rate_limiter.acquire()
        headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
        key = cache_key(
            url,
            kwargs.get("params"),
            headers.get("Accept", "") or self._client.headers.get("Accept", ""),
        )
        cached = self._conditional.get(key)
        if cached is not None:
            headers["If-None-Match"] = cached.etag
        response = self._client.get(url, headers=headers or None, **kwargs)
        if self._rate_limiter is not None:
            self._rate_limiter.update_from_headers(response.headers)
        if response.status_code == 304 and cached is not None:
            self._conditional.record_hit()
            return httpx.Response(
                200,
                headers=list(cached.headers),
                content=cached.content,
                request=response.request,
                extensions={"frank_not_modified": True},
            )
        etag = response.headers.get("etag")
        # isinstance, not truthiness: a test double's headers answer anything.
        if response.status_code == 200 and isinstance(etag, str) and etag:
            self._conditional.put(
                key,
                CachedReply(
                    etag=etag,
                    content=response.content,
                    headers=tuple(
                        (k, v)
                        for k, v in response.headers.items()
                        # The body is stored decoded; these described the wire form.
                        if k.lower()
                        not in ("content-encoding", "content-length", "transfer-encoding")
                    ),
                ),
            )
        elif cached is not None:
            self._conditional.discard(key)
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

    def list_pull_requests_page(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "closed",
        page: int = 1,
        per_page: int = 100,
        sort: str = "created",
    ) -> list[dict[str, Any]]:
        """One page of PRs in *state*, newest first by *sort*.

        No scrape fallback, unlike :meth:`list_pull_requests`: the search page
        the scrape reads has no stable paging and no ``merged_at``, and a walk
        through history that cannot tell merged from unmerged is worse than one
        that stops. A 401/403 raises and the caller says so.
        """
        response = self._get(
            f"/repos/{owner}/{repo}/pulls",
            params={
                "state": state,
                "sort": sort,
                "direction": "desc",
                "per_page": min(per_page, 100),
                "page": page,
            },
        )
        response.raise_for_status()
        data: list[dict[str, Any]] = response.json()
        return data

    supports_updated_since = True

    #: PRs per page of the changed-since listing. Small on purpose: the first page
    #: is the one re-asked every minute, a PR object is ~20 KB, and in a quiet
    #: minute the answer is a free 304 whatever the page size.
    _UPDATED_PAGE = 30

    def list_pull_requests_updated_since(
        self, owner: str, repo: str, since: datetime | None, *, max_pages: int = 10
    ) -> UpdatedPullRequests:
        """``state=all``, newest ``updated_at`` first, until one is older than *since*.

        The first page's URL is the same every time until something changes,
        so the conditional-request cache turns a quiet minute into a 304.
        """
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = self.list_pull_requests_page(
                owner, repo, state="all", page=page, per_page=self._UPDATED_PAGE, sort="updated"
            )
            for pr_data in data:
                updated = parse_github_timestamp(pr_data.get("updated_at"))
                if since is not None and updated is not None and updated < since:
                    return UpdatedPullRequests(items, complete=True)
                items.append(pr_data)
            if since is None or len(data) < self._UPDATED_PAGE:
                return UpdatedPullRequests(items, complete=True)
        return UpdatedPullRequests(items, complete=False)

    #: Notifications per page, and the most pages one check reads.
    _NOTIFICATION_PAGE = 50
    _NOTIFICATION_PAGES = 10

    def list_notifications(self, since: datetime | None) -> NotificationPage:
        """``GET /notifications?all=true&participating=true``, pull requests only.

        ``all=true`` so a notification the operator already read on github.com
        still counts — reading it there doesn't mean frank has seen it.
        ``participating=true`` is the "you were mentioned, asked, assigned, or
        are in the thread" subset, not every watched repository's traffic.
        """
        params: dict[str, str | int] = {
            "all": "true",
            "participating": "true",
            "per_page": self._NOTIFICATION_PAGE,
        }
        if since is not None:
            params["since"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        items: list[dict[str, Any]] = []
        poll_interval = 0
        complete = False
        for page in range(1, self._NOTIFICATION_PAGES + 1):
            response = self._get("/notifications", params={**params, "page": page})
            if response.status_code in (401, 403, 404) and not _is_rate_limit_response(response):
                message = ""
                with contextlib.suppress(Exception):
                    message = str(response.json().get("message", ""))
                raise NotificationsUnavailableError(response.status_code, message)
            response.raise_for_status()
            with contextlib.suppress(ValueError):
                poll_interval = int(response.headers.get("x-poll-interval", "0") or 0)
            data: list[dict[str, Any]] = response.json()
            items.extend(n for n in data if (n.get("subject") or {}).get("type") == "PullRequest")
            if len(data) < self._NOTIFICATION_PAGE:
                complete = True
                break
        return NotificationPage(items, poll_interval, complete)

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
        # Paginated: a long-lived PR on a busy repo passes 100 comments, and
        # the one that decides the intending-to-merge queue is the latest.
        found: list[dict[str, Any]] = []
        for page in range(1, 6):
            response = self._get(url, params={**params, "page": page})
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            found.extend(data)
            if len(data) < 100:
                break
        return found

    def list_pull_request_reviews(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """Every submitted review on the PR, paginated.

        GitHub's default page is 30 and a long-lived PR on a busy repo has
        more reviews than that; the one that matters for the
        intending-to-merge queue is usually the latest, i.e. the one a
        single page would drop.
        """
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        found: list[dict[str, Any]] = []
        for page in range(1, 6):
            response = self._get(url, params={"per_page": 100, "page": page})
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            found.extend(data)
            if len(data) < 100:
                break
        return found

    def get_commit_ci_status(self, owner: str, repo: str, sha: str) -> str:
        """Combine check runs and commit statuses for *sha* into one word.

        Both endpoints, because a repo may use either: GitHub Actions and
        most apps report check runs, Jenkins-style integrations (Spark's
        among them) still post commit statuses.
        """
        from franktheunicorn.worker.merge_intent import classify_ci

        if not sha:
            return ""
        # A large Actions matrix (Spark's is one) can pass 100 check runs on a
        # single commit, and a failure on page two is still a failure. Read to
        # the end; a commit with more than the cap is reported unknown rather
        # than green on the part that was read.
        check_runs: list[dict[str, Any]] = []
        max_pages = 10
        for page in range(1, max_pages + 1):
            checks_resp = self._get(
                f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
                params={"per_page": 100, "page": page},
            )
            checks_resp.raise_for_status()
            page_runs = checks_resp.json().get("check_runs") or []
            check_runs.extend(page_runs)
            if len(page_runs) < 100:
                break
        else:
            logger.warning(
                "%s/%s@%s has more than %d check runs; CI status left unknown",
                owner,
                repo,
                sha[:12],
                max_pages * 100,
            )
            return ""
        status_resp = self._get(
            f"/repos/{owner}/{repo}/commits/{sha}/status", params={"per_page": 100}
        )
        status_resp.raise_for_status()
        statuses = status_resp.json().get("statuses") or []
        return classify_ci(check_runs, statuses)

    def list_pull_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """Every inline review comment on the PR, not one review's worth.

        The review pipeline reads these so a finding that restates one becomes
        a ``+1`` instead of a second comment on the same line.
        """
        url = f"/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        found: list[dict[str, Any]] = []
        pages = 5
        truncated = False
        for page in range(1, pages + 1):
            response = self._get(url, params={"per_page": 100, "page": page})
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            found.extend(data)
            # The Link header, not a full last page: a PR with exactly 500
            # inline comments fills page five and has nothing after it, and
            # warning there would be the same confusion inverted. Read on every
            # page, before the break: a short page is the end of the list and
            # carries no next link, so this comes out False there — assigning
            # only on full pages would leak the previous page's rel="next"
            # through and warn on a fully-read thread.
            truncated = 'rel="next"' in response.headers.get("link", "")
            if len(data) < 100:
                break
        if truncated:
            # A thread this long means a finding could restate something we
            # never read. Say so rather than let the cap look like the end
            # of the list.
            logger.warning(
                "Read only the first %d inline comments on %s/%s#%d; a finding "
                "may restate one past that.",
                len(found),
                owner,
                repo,
                pr_number,
            )
        return found

    def get_commit_checks(self, owner: str, repo: str, sha: str) -> list[dict[str, Any]]:
        """Check runs plus commit statuses for ``sha``, in one normalised list.

        Two endpoints because GitHub has two CI models: Actions and most apps
        write check runs, older integrations (and anything using the Status
        API, e.g. Jenkins) write commit statuses. ``filter=latest`` is the
        check-runs default, so a re-run replaces its earlier attempt rather
        than appearing beside it. Past the same 1000-run cap as
        :meth:`get_commit_ci_status` it raises: a failure on an unread page
        is still a failure, so a partial list must not summarise as green.
        """
        checks: list[dict[str, Any]] = []
        runs_url = f"/repos/{owner}/{repo}/commits/{sha}/check-runs"
        max_pages = 10
        for page in range(1, max_pages + 1):
            response = self._get(runs_url, params={"per_page": 100, "page": page})
            response.raise_for_status()
            payload = response.json()
            runs = payload.get("check_runs", []) if isinstance(payload, dict) else []
            for run in runs:
                if not isinstance(run, dict) or not isinstance(run.get("name"), str):
                    continue
                checks.append(
                    {
                        "name": run["name"],
                        "status": str(run.get("status") or ""),
                        "conclusion": str(run.get("conclusion") or ""),
                        "url": str(run.get("html_url") or run.get("details_url") or ""),
                    }
                )
            if len(runs) < 100:
                break
        else:
            msg = f"{owner}/{repo}@{sha[:12]} has more than {max_pages * 100} check runs"
            raise RuntimeError(msg)

        status_resp = self._get(f"/repos/{owner}/{repo}/commits/{sha}/status")
        status_resp.raise_for_status()
        status_payload = status_resp.json()
        statuses = status_payload.get("statuses", []) if isinstance(status_payload, dict) else []
        for item in statuses:
            if not isinstance(item, dict) or not isinstance(item.get("context"), str):
                continue
            state = str(item.get("state") or "")
            checks.append(
                {
                    "name": item["context"],
                    "status": "completed" if state in ("success", "failure", "error") else "queued",
                    "conclusion": _STATUS_STATE_TO_CONCLUSION.get(state, ""),
                    "url": str(item.get("target_url") or ""),
                }
            )
        return checks

    def get_commit_check_outputs(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        """``check name -> failure output text`` for the failing check runs on ``sha``.

        Used by CI-failure analysis so an LLM can read *why* a check failed, not
        just that it did. Only the check-runs API carries ``output`` (title +
        summary); commit statuses (Jenkins et al.) have only a short
        ``description``, which ``get_commit_checks`` already surfaces via the
        url. Best-effort: a fetch failure yields no output for that check, which
        the caller treats as "analyse from the name alone".

        A GitHub Actions run's title/summary is usually just "Process completed
        with exit code 1", so for those the job log is fetched too and cut down
        to its error regions and tail (:func:`~franktheunicorn.scoring.ci_analysis.excerpt_log`)
        — that is where the reason is. At most ``_MAX_JOB_LOGS`` logs per call:
        each is one request and a Spark job log runs to megabytes.
        """
        from franktheunicorn.scoring.ci_analysis import excerpt_log

        outputs: dict[str, Any] = {}
        logs_fetched = 0
        runs_url = f"/repos/{owner}/{repo}/commits/{sha}/check-runs"
        for page in range(1, 11):
            try:
                response = self._get(runs_url, params={"per_page": 100, "page": page})
                response.raise_for_status()
            except Exception:
                logger.debug(
                    "Could not fetch check-run outputs for %s/%s@%s", owner, repo, sha[:12]
                )
                break
            payload = response.json()
            runs = payload.get("check_runs", []) if isinstance(payload, dict) else []
            for run in runs:
                if not isinstance(run, dict) or not isinstance(run.get("name"), str):
                    continue
                if run.get("conclusion") not in ("failure", "timed_out", "action_required"):
                    continue
                out = run.get("output")
                if not isinstance(out, dict):
                    out = {}
                title = str(out.get("title") or "")
                summary = str(out.get("summary") or "")
                text = f"{title}\n{summary}".strip()
                app = run.get("app")
                slug = app.get("slug") if isinstance(app, dict) else None
                linked = _linked_run(run, owner, repo)
                if linked is not None:
                    # The check mirrors a run in another repository (Spark's
                    # ``Build``): its own output is a link, and its id is not a
                    # job id here. The failed jobs of that run carry the logs.
                    for job in self._failed_run_jobs(*linked):
                        if logs_fetched >= _MAX_JOB_LOGS:
                            break
                        logs_fetched += 1
                        log = self._actions_job_log(linked[0], linked[1], int(job["id"]))
                        if log:
                            text = (
                                f"{text}\n\nLog excerpt ({job['name']}):\n{excerpt_log(log)}"
                            ).strip()
                elif slug == "github-actions" and run.get("id") and logs_fetched < _MAX_JOB_LOGS:
                    logs_fetched += 1
                    log = self._actions_job_log(owner, repo, int(run["id"]))
                    if log:
                        text = f"{text}\n\nLog excerpt:\n{excerpt_log(log)}".strip()
                if text:
                    outputs[run["name"]] = text
            if len(runs) < 100:
                break
        return outputs

    def get_commit_check_failures(self, owner: str, repo: str, sha: str) -> list[dict[str, Any]]:
        """What failed on ``sha``: one entry per failed *job*, with its log excerpt.

        ``get_commit_checks`` says a check is red; this says which job and step
        inside it, and what the log said. Each entry is ``{"check", "job",
        "step", "conclusion", "url", "title", "summary", "log", "linked_run"}``:
        ``check`` is the name on the PR, ``job`` the failed job when the check
        stands for a whole workflow run (else ``""``), ``step`` the first failed
        step when the forge reports steps, ``log`` an excerpt
        (:func:`~franktheunicorn.scoring.ci_analysis.excerpt_log`) or ``""``.

        The case this exists for is Apache Spark, where a PR's tests run in the
        *contributor's fork* and apache/spark carries one mirror check, ``Build``,
        whose output is a link and whose ``output.text`` is ``{"owner", "repo",
        "run_id"}`` naming the fork's run (``.github/workflows/
        update_build_status.js``). Reading that check alone says "Build failed";
        the fork run's jobs say "Linters, licenses, and dependencies failed at
        Scala linter". ``linked_run`` is ``"owner/repo#run_id"`` for those.

        A failing check that cannot be expanded (no linked run, the jobs call
        failed, a commit status) is still returned, by name, so the caller can
        say that it failed even when it cannot say why. At most ``_MAX_JOB_LOGS``
        logs per call. Raises when the check runs themselves cannot be listed:
        "nothing failed" and "could not look" must not read the same.
        """
        from franktheunicorn.scoring.ci_analysis import excerpt_log
        from franktheunicorn.scoring.merge_readiness import FAILED_CONCLUSIONS

        failures: list[dict[str, Any]] = []
        logs_fetched = 0

        def entry(run: dict[str, Any], **extra: Any) -> dict[str, Any]:
            out = run.get("output")
            out = out if isinstance(out, dict) else {}
            base: dict[str, Any] = {
                "check": run["name"],
                "job": "",
                "step": "",
                "conclusion": str(run.get("conclusion") or ""),
                "url": str(run.get("html_url") or run.get("details_url") or ""),
                "title": str(out.get("title") or ""),
                "summary": str(out.get("summary") or ""),
                "log": "",
                "linked_run": "",
            }
            base.update(extra)
            return base

        runs_url = f"/repos/{owner}/{repo}/commits/{sha}/check-runs"
        for page in range(1, 11):
            response = self._get(runs_url, params={"per_page": 100, "page": page})
            response.raise_for_status()
            payload = response.json()
            runs = payload.get("check_runs", []) if isinstance(payload, dict) else []
            for run in runs:
                if not isinstance(run, dict) or not isinstance(run.get("name"), str):
                    continue
                if run.get("conclusion") not in FAILED_CONCLUSIONS:
                    continue
                linked = _linked_run(run, owner, repo)
                # ``action_required`` on a mirror check is setup guidance, not a
                # run that failed: there are no jobs behind it to read.
                jobs = (
                    self._failed_run_jobs(*linked)
                    if linked is not None and run.get("conclusion") != "action_required"
                    else []
                )
                if linked is not None and jobs:
                    for job in jobs:
                        log = ""
                        if logs_fetched < _MAX_JOB_LOGS:
                            logs_fetched += 1
                            log = self._actions_job_log(linked[0], linked[1], int(job["id"]))
                        failures.append(
                            entry(
                                run,
                                job=job["name"],
                                step=job["step"],
                                conclusion=job["conclusion"],
                                url=job["url"] or str(run.get("details_url") or ""),
                                log=excerpt_log(log) if log else "",
                                linked_run=f"{linked[0]}/{linked[1]}#{linked[2]}",
                            )
                        )
                    continue
                app = run.get("app")
                slug = app.get("slug") if isinstance(app, dict) else None
                log = ""
                step = ""
                # A job never concludes ``action_required``; a check that does
                # was written through the Checks API (Spark's setup guidance)
                # and its id is not a job id, so there is no log to ask for.
                if (
                    linked is None
                    and slug == "github-actions"
                    and run.get("id")
                    and run.get("conclusion") != "action_required"
                    and logs_fetched < _MAX_JOB_LOGS
                ):
                    logs_fetched += 1
                    log = self._actions_job_log(owner, repo, int(run["id"]))
                    step = self._actions_job_failed_step(owner, repo, int(run["id"]))
                failures.append(
                    entry(
                        run,
                        step=step,
                        log=excerpt_log(log) if log else "",
                        linked_run=f"{linked[0]}/{linked[1]}#{linked[2]}" if linked else "",
                    )
                )
            if len(runs) < 100:
                break

        # Commit statuses (Jenkins and friends): a name, a one-line description
        # and a link. Best-effort — the check runs above are already an answer.
        try:
            status_resp = self._get(f"/repos/{owner}/{repo}/commits/{sha}/status")
            status_resp.raise_for_status()
            status_payload = status_resp.json()
        except Exception:
            logger.debug("Could not fetch commit statuses for %s/%s@%s", owner, repo, sha[:12])
            return failures
        statuses = status_payload.get("statuses", []) if isinstance(status_payload, dict) else []
        for item in statuses:
            if not isinstance(item, dict) or not isinstance(item.get("context"), str):
                continue
            if item.get("state") not in ("failure", "error"):
                continue
            failures.append(
                {
                    "check": item["context"],
                    "job": "",
                    "step": "",
                    "conclusion": "failure",
                    "url": str(item.get("target_url") or ""),
                    "title": "",
                    "summary": str(item.get("description") or ""),
                    "log": "",
                    "linked_run": "",
                }
            )
        return failures

    def _failed_run_jobs(self, owner: str, repo: str, run_id: int) -> list[dict[str, Any]]:
        """The failed jobs of one Actions run: ``[{"id", "name", "step", "conclusion", "url"}]``.

        ``filter=latest`` so a re-run's attempt replaces the one it retried.
        Failed and timed-out jobs first; a run that was only cancelled reports
        its cancelled jobs instead, so "somebody stopped it" is still an answer.
        Best-effort: any failure is an empty list, and the caller falls back to
        the check by name. Capped — a broken build fails every job in the matrix
        with the same error, and a dozen of them say it as well as forty.
        """
        failed: list[dict[str, Any]] = []
        cancelled: list[dict[str, Any]] = []
        url = f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
        for page in range(1, 4):
            try:
                response = self._get(
                    url, params={"filter": "latest", "per_page": 100, "page": page}
                )
            except Exception:
                logger.debug("Could not list jobs for %s/%s run %d", owner, repo, run_id)
                break
            if response.status_code != 200:
                logger.debug(
                    "No jobs for %s/%s run %d (HTTP %d)", owner, repo, run_id, response.status_code
                )
                break
            payload = response.json()
            jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
            for job in jobs:
                if not isinstance(job, dict) or not job.get("id"):
                    continue
                conclusion = str(job.get("conclusion") or "")
                if conclusion not in ("failure", "timed_out", "cancelled"):
                    continue
                row = {
                    "id": int(job["id"]),
                    "name": str(job.get("name") or ""),
                    "step": _first_failed_step(job.get("steps")),
                    "conclusion": conclusion,
                    "url": str(job.get("html_url") or ""),
                }
                (cancelled if conclusion == "cancelled" else failed).append(row)
            if len(jobs) < 100:
                break
        return (failed or cancelled)[:_MAX_FAILED_JOBS]

    def _actions_job_failed_step(self, owner: str, repo: str, job_id: int) -> str:
        """The name of the first failed step of an Actions job, or ``""``."""
        try:
            resp = self._get(f"/repos/{owner}/{repo}/actions/jobs/{job_id}")
        except Exception:
            return ""
        if resp.status_code != 200:
            return ""
        payload = resp.json()
        return _first_failed_step(payload.get("steps") if isinstance(payload, dict) else None)

    def _actions_job_log(self, owner: str, repo: str, job_id: int) -> str:
        """An Actions job's log text, or ``""`` when it can't be had.

        For an Actions check run the check-run id is the job id. The endpoint
        answers with a 302 to blob storage; httpx drops the auth header on the
        cross-origin hop, which is what the signed URL expects.
        """
        try:
            resp = self._get(
                f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs", follow_redirects=True
            )
        except Exception:
            logger.debug("Could not fetch the log for %s/%s job %d", owner, repo, job_id)
            return ""
        if resp.status_code != 200:
            logger.debug("No log for %s/%s job %d (HTTP %d)", owner, repo, job_id, resp.status_code)
            return ""
        return resp.text

    # -- Fork setup -----------------------------------------------------------

    def list_runs_awaiting_approval(
        self, owner: str, repo: str, head_sha: str = ""
    ) -> list[dict[str, Any]]:
        """Fork PR runs on ``owner/repo`` that wait for a maintainer to approve them.

        GitHub holds a ``pull_request`` run from a first-time contributor's fork
        until someone with write access approves it; such a run reads
        ``conclusion: action_required``, and the ``status`` filter matches
        conclusions too. Only runs whose head repository is not ``owner/repo``
        are kept: approval is a fork thing. Up to three pages.
        """
        params: dict[str, Any] = {"status": "action_required", "per_page": 100}
        if head_sha:
            params["head_sha"] = head_sha
        upstream = f"{owner}/{repo}".lower()
        found: list[dict[str, Any]] = []
        for page in range(1, 4):
            response = self._get(
                f"/repos/{owner}/{repo}/actions/runs", params={**params, "page": page}
            )
            response.raise_for_status()
            payload = response.json()
            runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
            for run in runs:
                if not isinstance(run, dict) or not isinstance(run.get("id"), int):
                    continue
                if run.get("conclusion") != "action_required":
                    continue
                head_repo = run.get("head_repository")
                head_name = head_repo.get("full_name") if isinstance(head_repo, dict) else ""
                if not isinstance(head_name, str) or head_name.lower() == upstream:
                    continue
                found.append(
                    {
                        "id": run["id"],
                        "name": str(run.get("name") or ""),
                        "path": str(run.get("path") or "").split("@", 1)[0],
                        "event": str(run.get("event") or ""),
                        "head_sha": str(run.get("head_sha") or ""),
                        "head_branch": str(run.get("head_branch") or ""),
                        "head_repo": head_name,
                        "url": str(run.get("html_url") or ""),
                        "created_at": str(run.get("created_at") or ""),
                    }
                )
            if len(runs) < 100:
                break
        return found

    def approve_workflow_run(self, owner: str, repo: str, run_id: int) -> None:
        """``POST /repos/{owner}/{repo}/actions/runs/{run_id}/approve`` (201 on success)."""
        response = self._client.post(f"/repos/{owner}/{repo}/actions/runs/{run_id}/approve")
        response.raise_for_status()

    def list_repo_workflows(self, owner: str, repo: str) -> list[dict[str, Any]] | None:
        """The repository's workflows and their state; ``None`` on 403/404."""
        workflows: list[dict[str, Any]] = []
        for page in range(1, 4):
            response = self._get(
                f"/repos/{owner}/{repo}/actions/workflows",
                params={"per_page": 100, "page": page},
            )
            if response.status_code in (403, 404) and not _is_rate_limit_response(response):
                return None
            response.raise_for_status()
            payload = response.json()
            items = payload.get("workflows", []) if isinstance(payload, dict) else []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    continue
                workflows.append(
                    {
                        "path": item["path"],
                        "name": str(item.get("name") or ""),
                        "state": str(item.get("state") or ""),
                        "url": str(item.get("html_url") or ""),
                    }
                )
            if len(items) < 100:
                break
        return workflows

    def list_workflow_runs_for_commit(
        self, owner: str, repo: str, sha: str
    ) -> list[dict[str, Any]]:
        """Runs for ``sha`` (one page of 100, newest first); ``[]`` on 403/404."""
        response = self._get(
            f"/repos/{owner}/{repo}/actions/runs", params={"head_sha": sha, "per_page": 100}
        )
        if response.status_code in (403, 404) and not _is_rate_limit_response(response):
            return []
        response.raise_for_status()
        payload = response.json()
        runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
        return [
            {
                "id": run["id"],
                "name": str(run.get("name") or ""),
                "path": str(run.get("path") or "").split("@", 1)[0],
                "event": str(run.get("event") or ""),
                "status": str(run.get("status") or ""),
                "conclusion": str(run.get("conclusion") or ""),
                "branch": str(run.get("head_branch") or ""),
                "url": str(run.get("html_url") or ""),
            }
            for run in runs
            if isinstance(run, dict) and isinstance(run.get("id"), int)
        ]

    def file_exists(self, owner: str, repo: str, path: str, ref: str) -> bool:
        """A contents read: 200 is yes, 404 is no, anything else raises."""
        response = self._get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def create_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict[str, Any]:
        """Post a conversation comment on a PR (the Issues API — PRs are issues)."""
        url = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        response = self._client.post(url, json={"body": body})
        response.raise_for_status()
        result: dict[str, Any] = response.json()
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

    def list_repo_collaborators(self, owner: str, repo: str, permission: str = "push") -> list[str]:
        """Everyone with *permission* or better on the repo, however they got it.

        ``affiliation=all`` counts access through a team and org ownership
        too, which is how ASF grants committers write access (and why the
        org's owners appear as well). GitHub answers only a token that itself
        has write access: anyone else gets 403, and a private repo the token
        cannot see is 404. Paginated to 2000 logins.
        """
        url = f"/repos/{owner}/{repo}/collaborators"
        logins: list[str] = []
        for page in range(1, 21):
            response = self._get(
                url,
                params={
                    "affiliation": "all",
                    "permission": permission,
                    "per_page": 100,
                    "page": page,
                },
            )
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            logins.extend(str(e["login"]) for e in data if isinstance(e, dict) and e.get("login"))
            if len(data) < 100:
                break
        return logins

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        """Members of ``org/team_slug``, child teams included, paginated to 2000.

        GitHub shows a team only to the org's members (a secret one only to
        its own members), and a classic token needs ``read:org``; anyone
        else gets 404 or 403.
        """
        url = f"/orgs/{org}/teams/{team_slug}/members"
        logins: list[str] = []
        for page in range(1, 21):
            response = self._get(url, params={"per_page": 100, "page": page})
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
            logins.extend(str(e["login"]) for e in data if isinstance(e, dict) and e.get("login"))
            if len(data) < 100:
                break
        return logins

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

    def search_prs_reviewed_by(self, username: str, max_results: int = 100) -> list[dict[str, Any]]:
        """Search for open PRs ``username`` has already reviewed.

        The same query as GitHub's own ``is:open is:pr reviewed-by:@me`` filter.
        ``@me`` is a UI convenience the REST search API doesn't take, so the
        resolved username goes in — which is why ``ensure_github_username`` runs
        before the first cycle.
        """
        return self._search_prs(
            f"reviewed-by:{username} type:pr state:open", max_results, what="reviewed-by scan"
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


# Commit-status states onto check-run conclusions, so both CI models read the
# same way downstream. "error" is a failure that never produced a verdict
# (infrastructure, usually), which is exactly the kind that flakes.
_STATUS_STATE_TO_CONCLUSION: dict[str, str] = {
    "success": "success",
    "failure": "failure",
    "error": "failure",
}


def parse_github_timestamp(value: object) -> datetime | None:
    """``2026-11-24T10:00:00Z`` → an aware datetime; None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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
