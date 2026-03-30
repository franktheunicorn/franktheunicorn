"""
PR polling service.

Loads configured projects, fetches PRs from GitHub (or mock),
scores them, and stores results in the database.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from django.db import transaction

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Project, PullRequest
from franktheunicorn.scoring.scorer import score_pull_request_from_model

logger = logging.getLogger(__name__)


class GitHubClientProtocol(Protocol):
    """Protocol for GitHub client (real or mock)."""

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]: ...

    def get_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


def poll_project(
    client: GitHubClientProtocol,
    project_config: ProjectConfig,
    operator_username: str,
) -> list[PullRequest]:
    """
    Poll a single project for PRs, score them, and store in the DB.

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

        # Score the PR
        score, breakdown = score_pull_request_from_model(
            pr=pr_obj,
            project_config=project_config,
            operator_username=operator_username,
        )
        pr_obj.interest_score = score
        pr_obj.score_breakdown = breakdown
        pr_obj.save(update_fields=["interest_score", "score_breakdown"])

        results.append(pr_obj)

    logger.info("Polled %s: %d PRs ingested/updated", project.full_name, len(results))
    return results


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

    pr_obj, _created = PullRequest.objects.update_or_create(
        project=project,
        number=pr_number,
        defaults=defaults,
    )
    return pr_obj
