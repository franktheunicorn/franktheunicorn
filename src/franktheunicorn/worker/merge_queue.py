"""Merge queue — track merge readiness and execute merges (v2).

Evaluates PRs for merge eligibility based on CI status, approvals,
and merge conflicts. Supports custom merge scripts (ala Spark).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from franktheunicorn.config.models import MergeQueueConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)


@dataclass
class MergeEligibility:
    """Checklist result for merge readiness."""

    eligible: bool = False
    ci_pass: bool = False
    approvals_met: bool = False
    no_conflicts: bool = False
    details: list[str] = field(default_factory=list)


@dataclass
class MergeResult:
    """Result of a merge attempt."""

    success: bool = False
    method: str = ""
    output: str = ""
    error: str = ""


def evaluate_merge_eligibility(
    pr: PullRequest,
    config: MergeQueueConfig,
) -> MergeEligibility:
    """Evaluate whether a PR is eligible for merging.

    Checks CI status, approval count, and merge conflict status.
    """
    result = MergeEligibility()

    # CI status check.
    if config.require_ci_pass:
        result.ci_pass = pr.ci_status == "pass"
        if not result.ci_pass:
            result.details.append(f"CI status: {pr.ci_status or 'unknown'} (requires pass)")
    else:
        result.ci_pass = True

    # Approval count check.
    result.approvals_met = pr.approval_count >= config.required_approvals
    if not result.approvals_met:
        result.details.append(f"Approvals: {pr.approval_count}/{config.required_approvals}")

    # Conflict check.
    if config.require_no_conflicts:
        # mergeable=None means GitHub hasn't computed it yet; treat as not ready.
        result.no_conflicts = pr.mergeable is True
        if not result.no_conflicts:
            status = "unknown" if pr.mergeable is None else "has conflicts"
            result.details.append(f"Merge status: {status}")
    else:
        result.no_conflicts = True

    result.eligible = result.ci_pass and result.approvals_met and result.no_conflicts
    return result


def update_merge_eligibility(
    pr: PullRequest,
    config: MergeQueueConfig,
) -> MergeEligibility:
    """Evaluate and persist merge eligibility on the PR."""
    eligibility = evaluate_merge_eligibility(pr, config)
    pr.merge_queue_eligible = eligibility.eligible
    pr.save(update_fields=["merge_queue_eligible", "updated_at"])
    return eligibility


def execute_merge_script(
    pr: PullRequest,
    script_path: str,
) -> MergeResult:
    """Execute a custom merge script for a PR.

    The script receives the PR number as the first argument and the
    full repo name (owner/repo) as the second.
    """
    cmd = [script_path, str(pr.number), pr.project.full_name]
    logger.info("Running merge script: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            return MergeResult(
                success=True,
                method="script",
                output=result.stdout,
            )
        return MergeResult(
            success=False,
            method="script",
            error=f"Script failed (exit {result.returncode}):\n{result.stderr}",
        )
    except FileNotFoundError:
        return MergeResult(
            success=False,
            method="script",
            error=f"Merge script not found: {script_path}",
        )
    except subprocess.TimeoutExpired:
        return MergeResult(
            success=False,
            method="script",
            error="Merge script timed out after 5 minutes",
        )


def execute_merge_api(
    pr: PullRequest,
    config: MergeQueueConfig,
    github_client: object,
    *,
    operator_username: str = "franktheunicorn",
) -> MergeResult:
    """Execute a merge via the GitHub API.

    Uses the configured merge method (merge, squash, rebase).
    """
    try:
        from django.db import transaction

        from franktheunicorn.backends.github import GitHubClient

        if not isinstance(github_client, GitHubClient):
            return MergeResult(
                success=False,
                method=config.merge_method,
                error="GitHub client not available (mock mode?)",
            )

        response = github_client._client.put(
            f"https://api.github.com/repos/{pr.project.full_name}/pulls/{pr.number}/merge",
            json={"merge_method": config.merge_method},
        )
        if response.status_code == 200:
            with transaction.atomic():
                pr.state = "merged"
                pr.merged_at = timezone.now()
                pr.merged_by = operator_username
                pr.save(update_fields=["state", "merged_at", "merged_by", "updated_at"])
            return MergeResult(
                success=True,
                method=config.merge_method,
                output=f"Merged PR #{pr.number} via {config.merge_method}",
            )
        return MergeResult(
            success=False,
            method=config.merge_method,
            error=f"GitHub API returned {response.status_code}: {response.text}",
        )
    except Exception as exc:
        return MergeResult(
            success=False,
            method=config.merge_method,
            error=str(exc),
        )


def execute_merge(
    pr: PullRequest,
    config: MergeQueueConfig,
    github_client: object | None = None,
) -> MergeResult:
    """Execute a merge using the configured method.

    If a merge script is configured, uses that. Otherwise falls back
    to the GitHub API.
    """
    if config.merge_script:
        return execute_merge_script(pr, config.merge_script)
    if github_client is not None:
        return execute_merge_api(pr, config, github_client)
    return MergeResult(
        success=False,
        error="No merge method available (no script and no GitHub client)",
    )
