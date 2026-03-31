"""
PR polling service.

Loads configured projects, fetches PRs from GitHub (or mock),
scores them, and stores results in the database.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from django.db import transaction

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Project, PullRequest, ReviewDraft
from franktheunicorn.core.session_detector import detect_agent_session
from franktheunicorn.scoring.scorer import score_pull_request_from_model

logger = logging.getLogger(__name__)


class GitHubClientProtocol(Protocol):
    """Protocol for GitHub client (real or mock)."""

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]: ...

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]: ...

    def get_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]: ...

    def get_issue_comments(
        self, owner: str, repo: str, issue_number: int, since: str | None = None
    ) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


def poll_project(
    client: GitHubClientProtocol,
    project_config: ProjectConfig,
    operator_username: str,
    *,
    repo_path: Path | None = None,
) -> list[PullRequest]:
    """
    Poll a single project for PRs, score them, and store in the DB.

    If repo_path is provided and points to a valid git clone, blame data
    is fetched for changed files and passed to the scorer.

    Returns the list of PullRequest objects that were created or updated.
    """
    project, _created = Project.objects.update_or_create(
        owner=project_config.owner,
        repo=project_config.repo,
        defaults={"review_context": project_config.review_context},
    )

    raw_prs = client.list_pull_requests(project_config.owner, project_config.repo)
    results: list[PullRequest] = []

    for pr_data in raw_prs:
        pr_number: int = pr_data["number"]

        # Fetch changed files for scoring
        try:
            files_data = client.get_pull_request_files(
                project_config.owner, project_config.repo, pr_number
            )
            changed_files = [f["filename"] for f in files_data]
        except Exception:
            logger.warning("Could not fetch files for PR #%d, using empty list", pr_number)
            changed_files = []

        pr_obj = _upsert_pull_request(project, pr_data, changed_files)

        # Fetch PR detail (includes mergeable status + base/head refs).
        pr_detail: dict[str, Any] = {}
        try:
            pr_detail = client.get_pull_request(
                project_config.owner, project_config.repo, pr_number
            )
            raw_mergeable = pr_detail.get("mergeable")
            if isinstance(raw_mergeable, bool):
                pr_obj.mergeable = raw_mergeable
        except Exception:
            logger.debug("Could not fetch PR detail for #%d", pr_number)

        # Extract base/head SHAs for blame (v1.25).
        base_sha = ""
        head_sha = ""
        base_data = pr_detail.get("base")
        head_data = pr_detail.get("head")
        if isinstance(base_data, dict):
            base_sha = base_data.get("sha", "")
        if isinstance(head_data, dict):
            head_sha = head_data.get("sha", "")

        # Fetch blame data if repo clone is available (v1.25).
        blame_data: list[dict[str, object]] | None = None
        if repo_path is not None and repo_path.is_dir() and changed_files and base_sha:
            try:
                from franktheunicorn.scoring.blame_fetcher import fetch_blame_for_files
                from franktheunicorn.worker.repo_manager import ensure_ref_available

                # Verify both refs are available locally before running blame.
                base_ok = ensure_ref_available(repo_path, base_sha)
                head_ok = ensure_ref_available(repo_path, head_sha) if head_sha else False

                if base_ok and head_ok:
                    blame_data = fetch_blame_for_files(
                        repo_path, changed_files, base_ref=base_sha, head_ref=head_sha
                    )
                elif base_ok:
                    # Head not available but base is — diff will be against
                    # working tree. Only useful if repo happens to be on the
                    # right branch, so log a warning.
                    logger.debug(
                        "Head SHA %s not available locally for PR #%d; "
                        "blame diff may be inaccurate",
                        head_sha[:12],
                        pr_number,
                    )
                    blame_data = fetch_blame_for_files(repo_path, changed_files, base_ref=base_sha)
                else:
                    logger.debug(
                        "Base SHA %s not available locally for PR #%d; skipping blame",
                        base_sha[:12],
                        pr_number,
                    )
            except Exception:
                logger.debug("Blame fetch failed for PR #%d", pr_number, exc_info=True)

        # Gather re-engagement data: check if operator has posted reviews
        # and if the author has replied since.
        operator_review_posted_at: str | None = None
        author_replies: list[str] | None = None

        latest_posted_at = (
            ReviewDraft.objects.filter(
                pull_request=pr_obj, status="posted", posted_at__isnull=False
            )
            .order_by("-posted_at")
            .values_list("posted_at", flat=True)
            .first()
        )
        if latest_posted_at is not None:
            operator_review_posted_at = latest_posted_at.isoformat()
            try:
                issue_comments = client.get_issue_comments(
                    project_config.owner,
                    project_config.repo,
                    pr_number,
                    since=operator_review_posted_at,
                )
                pr_author = pr_obj.author.lower()
                author_replies = [
                    c["created_at"]
                    for c in issue_comments
                    if c.get("user", {}).get("login", "").lower() == pr_author
                ]
            except Exception:
                logger.debug("Could not fetch comments for PR #%d", pr_number)

        # Score the PR
        score, breakdown = score_pull_request_from_model(
            pr=pr_obj,
            project_config=project_config,
            operator_username=operator_username,
            blame_data=blame_data,
            operator_review_posted_at=operator_review_posted_at,
            author_replies_after_review=author_replies,
        )
        pr_obj.interest_score = score
        pr_obj.score_breakdown = breakdown

        # Compute moderation flags and route to queue (§2.2).
        _route_pr_to_queue(pr_obj, operator_username)

        pr_obj.save(
            update_fields=[
                "interest_score",
                "score_breakdown",
                "queue",
                "is_operator_pr",
                "is_new_contributor",
                "is_low_context",
                "is_likely_unowned",
                "mergeable",
            ]
        )

        results.append(pr_obj)

    logger.info("Polled %s: %d PRs ingested/updated", project.full_name, len(results))
    return results


def _route_pr_to_queue(pr_obj: PullRequest, operator_username: str) -> None:
    """Set queue and boolean flags based on moderation flags."""
    from franktheunicorn.scoring.moderation import compute_moderation_flags

    pr_dict: dict[str, object] = {
        "author": pr_obj.author,
        "is_draft": pr_obj.is_draft,
        "additions": pr_obj.additions,
        "deletions": pr_obj.deletions,
        "body": pr_obj.body,
        "labels": pr_obj.labels,
        "changed_files": pr_obj.changed_files,
        "requested_reviewers": pr_obj.requested_reviewers,
    }
    if pr_obj.github_created_at:
        from datetime import UTC, datetime

        age = (datetime.now(tz=UTC) - pr_obj.github_created_at).days
        pr_dict["pr_age_days"] = age

    known_authors = list(
        PullRequest.objects.filter(project=pr_obj.project)
        .exclude(pk=pr_obj.pk)
        .values_list("author", flat=True)
        .distinct()
    )

    flags = compute_moderation_flags(pr_dict, operator_username, known_authors)

    pr_obj.is_operator_pr = "is_operator_pr" in flags
    pr_obj.is_new_contributor = "new_contributor" in flags
    pr_obj.is_low_context = "low_context" in flags
    pr_obj.is_likely_unowned = "likely_unowned" in flags

    # Route to queue based on priority of flags.
    if pr_obj.is_operator_pr:
        pr_obj.queue = "your-prs"
    elif pr_obj.likely_ai_generated or "bot" in flags:
        pr_obj.queue = "ai-generated"
    elif pr_obj.is_new_contributor:
        pr_obj.queue = "new-contributor"
    elif pr_obj.is_likely_unowned:
        pr_obj.queue = "consider-closing"
    elif pr_obj.is_low_context:
        pr_obj.queue = "needs-triage"
    else:
        pr_obj.queue = "review"


def _parse_github_datetime(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


@transaction.atomic
def _upsert_pull_request(
    project: Project,
    pr_data: dict[str, Any],
    changed_files: list[str],
) -> PullRequest:
    """Create or update a PullRequest from raw GitHub API data."""
    pr_number: int = pr_data["number"]
    user_data = pr_data.get("user") or {}

    defaults = {
        "github_id": pr_data.get("id", 0),
        "title": pr_data.get("title", ""),
        "author": user_data.get("login", "unknown"),
        "state": pr_data.get("state", "open"),
        "url": pr_data.get("html_url", ""),
        "diff_url": pr_data.get("diff_url", ""),
        "body": pr_data.get("body", "") or "",
        "labels": [lbl.get("name", "") for lbl in pr_data.get("labels", [])],
        "requested_reviewers": [r.get("login", "") for r in pr_data.get("requested_reviewers", [])],
        "assignees": [a.get("login", "") for a in pr_data.get("assignees", [])],
        "changed_files": changed_files,
        "additions": pr_data.get("additions", 0),
        "deletions": pr_data.get("deletions", 0),
        "is_draft": pr_data.get("draft", False),
        "github_created_at": _parse_github_datetime(pr_data.get("created_at")),
        "github_updated_at": _parse_github_datetime(pr_data.get("updated_at")),
    }

    # Detect AI agent session from PR description (v1.25).
    # Always set these fields so stale values are cleared on update.
    body = defaults["body"]
    session = detect_agent_session(body) if body else None
    defaults["ai_agent_source"] = session.agent_source if session else ""
    defaults["agent_session_url"] = session.session_url if session else ""
    defaults["agent_task_id"] = session.task_id if session else ""
    if session:
        defaults["likely_ai_generated"] = True

    pr_obj, _created = PullRequest.objects.update_or_create(
        project=project,
        number=pr_number,
        defaults=defaults,
    )
    return pr_obj
