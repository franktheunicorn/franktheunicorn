"""GitHub polling service.

Runs on an interval, loads project configs, polls GitHub for open PRs,
scores them, and stores results in SQLite.
"""

from __future__ import annotations

import json
import logging

from franktheunicorn.config import (
    OperatorConfig,
    ProjectConfig,
    load_operator_config,
    load_project_configs,
)
from franktheunicorn.database import get_db
from franktheunicorn.github_client import GitHubClient, GitHubPR
from franktheunicorn.scoring import ScoringContext, score_pr
from franktheunicorn.storage import upsert_project, upsert_pull_request

logger = logging.getLogger(__name__)


def _build_scoring_context(
    pr: GitHubPR,
    changed_files: list[str],
    operator: OperatorConfig,
    project: ProjectConfig,
    mentions: list[str],
) -> ScoringContext:
    return ScoringContext(
        author_login=pr.author_login,
        body=pr.body,
        labels=pr.labels,
        requested_reviewers=pr.requested_reviewers,
        changed_files=changed_files,
        updated_at=pr.updated_at,
        operator=operator,
        project=project,
        mentions_in_comments=mentions,
    )


def poll_project(
    client: GitHubClient,
    project_config: ProjectConfig,
    operator: OperatorConfig,
) -> int:
    """Poll a single project for open PRs.  Returns number of PRs processed."""
    logger.info("Polling %s (%s)", project_config.slug, project_config.repo)
    prs = client.list_open_prs(project_config.repo, per_page=project_config.max_prs_per_poll)
    if not prs:
        logger.info("No open PRs for %s", project_config.repo)
        return 0

    processed = 0
    with get_db() as session:
        project = upsert_project(session, project_config)
        session.flush()  # ensure project.id is populated

        for pr in prs:
            try:
                changed_files = client.list_pr_files(project_config.repo, pr.number)
                comments = client.get_issue_comments(project_config.repo, pr.number)
                mentions = [c["user"]["login"] for c in comments if c.get("user")]

                ctx = _build_scoring_context(pr, changed_files, operator, project_config, mentions)
                score_result = score_pr(ctx)

                upsert_pull_request(session, project, pr, score_result, changed_files)
                processed += 1
            except Exception:
                logger.exception("Error processing PR #%d in %s", pr.number, project_config.repo)

    logger.info("Processed %d PRs for %s", processed, project_config.slug)
    return processed


def run_poll_cycle(
    operator_config_path: str | None = None,
    projects_config_dir: str | None = None,
) -> int:
    """Run one full poll cycle across all configured projects.

    Returns total number of PRs processed.
    """
    operator = load_operator_config(operator_config_path)
    projects = load_project_configs(projects_config_dir)
    enabled = [p for p in projects if p.enabled]

    if not enabled:
        logger.warning("No enabled projects found.  Check your configs/ directory.")
        return 0

    logger.info("Poll cycle started: %d projects", len(enabled))
    total = 0
    with GitHubClient() as client:
        for project in enabled:
            try:
                total += poll_project(client, project, operator)
            except Exception:
                logger.exception("Unhandled error polling project %s", project.slug)

    logger.info("Poll cycle complete: %d PRs processed", total)
    return total


def get_stored_changed_files(pr_json: str) -> list[str]:
    """Deserialise changed files from JSON string stored in DB."""
    try:
        result = json.loads(pr_json)
        if isinstance(result, list):
            return [str(f) for f in result]
    except (json.JSONDecodeError, TypeError):
        pass
    return []
