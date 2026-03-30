"""
Worker runner — polls GitHub on an interval and processes PRs.

This is the main loop for the worker service. It:
1. Loads operator and project configs from YAML
2. Creates a GitHub client (real or mock)
3. Polls each project for PRs
4. Scores and stores results
5. Runs the stub review drafter on new/updated PRs
6. Sleeps and repeats
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import django

if TYPE_CHECKING:
    from franktheunicorn.config.models import CodeRabbitConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)


def run_worker() -> None:
    """Main worker entry point."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "franktheunicorn.settings")
    django.setup()

    from django.conf import settings

    from franktheunicorn.config.loader import load_operator_config, load_project_configs
    from franktheunicorn.github.client import GitHubClient
    from franktheunicorn.github.mock import MockGitHubClient

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    operator_config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
    project_configs = load_project_configs(settings.FRANK_PROJECTS_DIR)

    if not project_configs:
        logger.warning("No project configs found in %s", settings.FRANK_PROJECTS_DIR)
        logger.info("The worker will keep running and check again each cycle.")

    # Choose client based on mock mode
    client: GitHubClient | MockGitHubClient
    if settings.FRANK_MOCK_MODE:
        logger.info("Running in MOCK mode — using fixture data")
        client = MockGitHubClient(settings.FRANK_FIXTURES_DIR)
    else:
        if not settings.FRANK_GITHUB_TOKEN:
            logger.error("FRANK_GITHUB_TOKEN not set and mock mode is off. Exiting.")
            sys.exit(1)
        logger.info("Running with live GitHub API")
        client = GitHubClient(token=settings.FRANK_GITHUB_TOKEN)

    poll_interval = operator_config.poll_interval_seconds or settings.FRANK_POLL_INTERVAL
    logger.info("Worker starting. Poll interval: %ds", poll_interval)

    try:
        while True:
            _run_cycle(client, project_configs, operator_config.github_username, operator_config)
            logger.info("Sleeping %ds until next poll...", poll_interval)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("Worker shutting down.")
    finally:
        client.close()


def _run_cycle(
    client: object,
    project_configs: Sequence[object],
    operator_username: str,
    operator_config: object | None = None,
) -> None:
    """Run one polling cycle across all configured projects."""
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.github.poller import poll_project
    from franktheunicorn.review.drafter import draft_review

    # Resolve CodeRabbit config from operator config.
    cr_config: CodeRabbitConfig | None = None
    if isinstance(operator_config, OperatorConfig) and operator_config.coderabbit.enabled:
        cr_config = operator_config.coderabbit

    for pc in project_configs:
        if not isinstance(pc, ProjectConfig) or not pc.enabled:
            continue
        try:
            logger.info("Polling %s/%s ...", pc.owner, pc.repo)
            prs = poll_project(
                client=client,  # type: ignore[arg-type]
                project_config=pc,
                operator_username=operator_username,
            )
            for pr in prs:
                # Only draft reviews for PRs without existing drafts
                if not pr.review_drafts.exists():
                    drafts = draft_review(pr, pc)
                    logger.info(
                        "  PR #%d: score=%.2f, %d drafts generated",
                        pr.number,
                        pr.interest_score,
                        len(drafts),
                    )

                    # Run CodeRabbit if enabled and no CR drafts exist yet.
                    if cr_config is not None:
                        _run_coderabbit_for_pr(pr, cr_config)
        except Exception:
            logger.exception("Error polling %s/%s", pc.owner, pc.repo)


def _run_coderabbit_for_pr(
    pr: PullRequest,
    cr_config: CodeRabbitConfig,
) -> None:
    """Run CodeRabbit CLI review for a single PR. Never raises."""
    from franktheunicorn.review.coderabbit import (
        create_drafts_from_coderabbit,
        run_coderabbit_review,
    )

    try:
        # Derive repo clone path from the project. The worker is expected
        # to operate inside a local clone or have access to one.
        repo_path = Path.home() / ".review-agent" / "repos" / pr.project.full_name
        if not repo_path.exists():
            logger.debug(
                "Repo clone not found at %s; skipping CodeRabbit for PR #%d",
                repo_path,
                pr.number,
            )
            return

        # TODO(v1.5): Determine proper merge base from PR metadata once we
        # have local checkout support. For now, require the repo clone to be
        # checked out to the PR branch with a meaningful base available.
        # We skip rather than guess with HEAD~1, which would produce garbage.
        base_ref = _resolve_base_ref(repo_path, pr)
        if base_ref is None:
            return

        findings = run_coderabbit_review(repo_path, base_ref, cr_config)
        if findings:
            drafts = create_drafts_from_coderabbit(pr, findings, pr.project)
            logger.info(
                "  PR #%d: %d CodeRabbit findings → %d drafts",
                pr.number,
                len(findings),
                len(drafts),
            )
    except Exception:
        logger.exception("CodeRabbit failed for PR #%d; continuing.", pr.number)


def _resolve_base_ref(repo_path: Path, pr: PullRequest) -> str | None:
    """
    Try to determine the base ref for CodeRabbit diffing.

    Returns ``None`` (and logs) when we can't determine a sensible base.
    """
    import subprocess

    for candidate in ("origin/main", "origin/master"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
        )
        if result.returncode == 0:
            return candidate

    logger.debug(
        "Could not determine base ref for PR #%d in %s; skipping CodeRabbit.",
        pr.number,
        repo_path,
    )
    return None


if __name__ == "__main__":
    run_worker()
