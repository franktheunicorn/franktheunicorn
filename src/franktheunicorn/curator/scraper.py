"""Scrape historical review comments from GitHub for voice curation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


@dataclass
class RawComment:
    """A historical review comment with context."""

    author: str
    body: str
    diff_context: str
    file_path: str
    pr_number: int
    pr_title: str
    created_at: str
    url: str


def scrape_review_comments(
    owner: str,
    repo: str,
    token: str,
    limit: int = 100,
    *,
    author: str | None = None,
) -> list[RawComment]:
    """Scrape recent review comments from GitHub.

    Uses the GitHub API to fetch pull request review comments.
    Returns up to ``limit`` comments, sorted by most recent first.

    When ``author`` is provided, only comments by that GitHub username are
    returned.  Filtering happens client-side because the GitHub pulls/comments
    endpoint does not support an author filter.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    comments: list[RawComment] = []
    per_page = min(limit, 100)
    page = 1

    with httpx.Client(timeout=30.0) as client:
        while len(comments) < limit:
            url = (
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/comments"
                f"?sort=created&direction=desc&per_page={per_page}&page={page}"
            )
            response = client.get(url, headers=headers)
            response.raise_for_status()
            items = response.json()

            if not items:
                break

            for item in items:
                if len(comments) >= limit:
                    break
                comment_author = item.get("user", {}).get("login", "")
                if author is not None and comment_author != author:
                    continue
                comments.append(
                    RawComment(
                        author=comment_author,
                        body=item.get("body", "") or "",
                        diff_context=item.get("diff_hunk", "") or "",
                        file_path=item.get("path", "") or "",
                        pr_number=_extract_pr_number(item.get("pull_request_url", "")),
                        pr_title="",  # Not available from this endpoint
                        created_at=item.get("created_at", ""),
                        url=item.get("html_url", ""),
                    )
                )

            page += 1

    logger.info("Scraped %d review comments from %s/%s", len(comments), owner, repo)
    return comments


def scrape_user_comments(
    username: str,
    repos: list[tuple[str, str]],
    token: str,
    limit: int = 200,
) -> list[RawComment]:
    """Scrape review comments authored by a specific GitHub user across repos.

    ``repos`` is a list of ``(owner, repo)`` pairs.  Comments are fetched from
    each repo and filtered to those authored by ``username``.  Returns up to
    ``limit`` comments in total, distributed across repos.
    """
    if not repos:
        return []

    per_repo_limit = max(1, limit // len(repos))
    all_comments: list[RawComment] = []

    for owner, repo in repos:
        if len(all_comments) >= limit:
            break
        remaining = limit - len(all_comments)
        try:
            comments = scrape_review_comments(
                owner,
                repo,
                token,
                limit=min(per_repo_limit, remaining),
                author=username,
            )
            all_comments.extend(comments)
        except Exception:
            logger.warning(
                "Failed to scrape comments from %s/%s — skipping.", owner, repo, exc_info=True
            )

    logger.info(
        "Scraped %d comments by %s across %d repos", len(all_comments), username, len(repos)
    )
    return all_comments


def _extract_pr_number(pull_request_url: str) -> int:
    """Extract PR number from a pull_request_url like .../pulls/42."""
    if not pull_request_url:
        return 0
    parts = pull_request_url.rstrip("/").split("/")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return 0
