"""Merge queue — track merge readiness and execute merges (v2).

Evaluates PRs for merge eligibility based on CI status, approvals,
and merge conflicts. Supports custom merge scripts (ala Spark).
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    ci_wait_state: str = ""
    ci_wait_reason: str = ""


@dataclass
class RestackStepResult:
    """Single restack step execution result with logs."""

    name: str
    success: bool
    stdout: str = ""
    stderr: str = ""
    command: list[str] = field(default_factory=list)


@dataclass
class RestackExecutionResult:
    """Result for post-merge restack orchestration."""

    success: bool = False
    pr_number: int | None = None
    branch: str = ""
    target_branch: str = "main"
    steps: list[RestackStepResult] = field(default_factory=list)
    error: str = ""
    ci_wait_state: str = ""
    ci_wait_reason: str = ""


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
    *,
    repo_path: str = "",
) -> MergeResult:
    """Execute a merge using the configured method.

    If a merge script is configured, uses that. Otherwise falls back
    to the GitHub API.
    """
    if config.merge_script:
        merge_result = execute_merge_script(pr, config.merge_script)
    elif github_client is not None:
        merge_result = execute_merge_api(pr, config, github_client)
    else:
        return MergeResult(
            success=False,
            error="No merge method available (no script and no GitHub client)",
        )

    restack_enabled = config.restack_enabled or config.post_merge_restack_enabled
    if merge_result.success and restack_enabled and repo_path:
        restack_result = execute_post_merge_restack(
            pr,
            config,
            repo_path,
            github_client=github_client,
        )
        merge_result.ci_wait_state = restack_result.ci_wait_state
        merge_result.ci_wait_reason = restack_result.ci_wait_reason
        if not restack_result.success:
            merge_result.success = False
            merge_result.error = restack_result.error
            merge_result.output = "\n".join(
                [
                    merge_result.output,
                    f"post_merge_restack_failed for PR #{restack_result.pr_number}",
                ]
            ).strip()
    return merge_result


def select_next_pr_to_restack(project_id: int) -> PullRequest | None:
    """Select next PR to restack in queue order after a merge.

    Queue order: highest interest_score first, then oldest GitHub update.
    """
    from franktheunicorn.core.models import PullRequest

    return (
        PullRequest.objects.filter(
            project_id=project_id,
            state="open",
            merge_queue_eligible=True,
        )
        .order_by("-interest_score", "github_updated_at", "number")
        .first()
    )


def _run_git_step(name: str, cmd: list[str], cwd: Path) -> RestackStepResult:
    """Run a restack subprocess step and capture output."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    return RestackStepResult(
        name=name,
        success=result.returncode == 0,
        stdout=result.stdout,
        stderr=result.stderr,
        command=cmd,
    )


def _delete_stale_migrations_step(
    target: str,
    migration_globs: list[str],
    repo_dir: Path,
) -> RestackStepResult:
    """Delete migration files added on the branch that aren't on ``target``.

    Pure Python file deletion driven by ``git diff --name-only -z``. We
    intentionally avoid ``bash -lc`` + ``xargs rm`` here so glob patterns
    and branch names containing shell metacharacters can't escape the
    intended command.
    """
    diff_cmd: list[str] = [
        "git",
        "diff",
        "--name-only",
        "-z",
        f"origin/{target}...HEAD",
        "--",
        *migration_globs,
    ]
    result = subprocess.run(diff_cmd, cwd=repo_dir, capture_output=True, text=True, timeout=120)
    step = RestackStepResult(
        name="delete_stale_migrations",
        success=result.returncode == 0,
        stdout=result.stdout,
        stderr=result.stderr,
        command=diff_cmd,
    )
    if not step.success:
        return step

    repo_root = repo_dir.resolve()
    deleted: list[str] = []
    for relpath in result.stdout.split("\0"):
        if not relpath:
            continue
        candidate = (repo_dir / relpath).resolve()
        try:
            candidate.relative_to(repo_root)
        except ValueError:
            logger.warning("Skipping stale-migration path outside repo: %s", relpath)
            continue
        try:
            candidate.unlink()
            deleted.append(relpath)
        except FileNotFoundError:
            # ``git diff`` listed a path that's already gone (rename, manual
            # cleanup, etc.). Not an error — there's nothing to delete.
            continue
        except OSError as exc:
            # Permission denied / read-only FS / inode locks etc. Bail out:
            # downstream steps assume these files are gone, and pushing a
            # restacked branch with leftover migrations creates a worse
            # state than failing here.
            logger.error("Could not delete stale migration %s: %s", relpath, exc)
            step.success = False
            step.stderr = (step.stderr + f"\nfailed to delete {relpath}: {exc}").strip()
            return step
    if deleted:
        step.stdout = (step.stdout + "\n" + "\n".join(deleted)).strip()
    return step


def wait_for_ci_green(
    pr: PullRequest,
    github_client: object,
    timeout: int,
    poll_interval: int,
) -> tuple[str, str]:
    """Poll GitHub until required checks are successful, failed, or timed out."""
    from franktheunicorn.backends.github import GitHubClient

    if not isinstance(github_client, GitHubClient):
        return ("failure", "GitHub client unavailable for CI status polling")

    deadline = time.monotonic() + timeout
    repo = pr.project.full_name
    required_contexts: set[str] = set()
    default_branch = "main"

    while time.monotonic() < deadline:
        repo_resp = github_client._client.get(f"https://api.github.com/repos/{repo}")
        if repo_resp.status_code == 200:
            repo_data = repo_resp.json()
            fetched_default = repo_data.get("default_branch")
            if isinstance(fetched_default, str) and fetched_default:
                default_branch = fetched_default

        branch_resp = github_client._client.get(
            f"https://api.github.com/repos/{repo}/branches/{default_branch}"
        )
        if branch_resp.status_code == 200:
            branch_data = branch_resp.json()
            protection = branch_data.get("protection") or {}
            required = protection.get("required_status_checks") or {}
            contexts = required.get("contexts") or []
            required_contexts = {ctx for ctx in contexts if isinstance(ctx, str)}

        checks_resp = github_client._client.get(
            f"https://api.github.com/repos/{repo}/commits/{pr.head_sha}/check-runs"
        )
        statuses_resp = github_client._client.get(
            f"https://api.github.com/repos/{repo}/commits/{pr.head_sha}/status"
        )
        if checks_resp.status_code != 200 or statuses_resp.status_code != 200:
            return ("failure", "Failed to fetch GitHub CI check status")

        check_runs = checks_resp.json().get("check_runs", [])
        status_data = statuses_resp.json().get("statuses", [])

        check_run_by_name = {
            run.get("name"): run
            for run in check_runs
            if isinstance(run, dict) and isinstance(run.get("name"), str)
        }
        status_by_context = {
            item.get("context"): item.get("state")
            for item in status_data
            if isinstance(item, dict) and isinstance(item.get("context"), str)
        }

        pending_contexts: list[str] = []
        for context in sorted(required_contexts):
            run = check_run_by_name.get(context)
            if run is not None:
                conclusion = run.get("conclusion")
                if conclusion == "success":
                    continue
                if conclusion in {"failure", "timed_out", "cancelled", "action_required"}:
                    return ("failure", f"Required check failed: {context} ({conclusion})")
                pending_contexts.append(context)
                continue

            state = status_by_context.get(context)
            if state == "success":
                continue
            if state in {"failure", "error"}:
                return ("failure", f"Required check failed: {context} ({state})")
            pending_contexts.append(context)

        if not pending_contexts:
            return ("success", "All required checks passed")
        time.sleep(poll_interval)

    return ("timeout", "Timed out waiting for required checks to pass")


def execute_post_merge_restack(
    merged_pr: PullRequest,
    config: MergeQueueConfig,
    repo_path: str,
    github_client: object | None = None,
) -> RestackExecutionResult:
    """Restack next queue PR branch after a successful merge."""
    if not config.restack_enabled:
        return RestackExecutionResult(success=True, target_branch=config.restack_target_branch)
    next_pr = select_next_pr_to_restack(merged_pr.project_id)
    if next_pr is None:
        return RestackExecutionResult(success=True, target_branch=config.restack_target_branch)

    branch_name = f"pr-{next_pr.number}"
    target = config.restack_target_branch or "main"
    repo_dir = Path(repo_path)
    execution = RestackExecutionResult(
        pr_number=next_pr.number,
        branch=branch_name,
        target_branch=target,
    )
    checkout_step = _run_git_step(
        "checkout_pr_branch",
        ["git", "checkout", "-B", branch_name, f"origin/{branch_name}"],
        repo_dir,
    )
    execution.steps.append(checkout_step)
    if not checkout_step.success:
        execution.success = False
        execution.error = "Step failed: checkout_pr_branch"
        return execution

    if config.delete_stale_migrations:
        delete_step = _delete_stale_migrations_step(target, list(config.migration_globs), repo_dir)
        execution.steps.append(delete_step)
        if not delete_step.success:
            execution.success = False
            execution.error = "Step failed: delete_stale_migrations"
            return execution

    steps: list[tuple[str, list[str]]] = [
        ("rebase_onto_target", ["git", "rebase", f"origin/{target}"]),
        ("regenerate_migrations", ["python", "manage.py", "makemigrations"]),
        (
            "commit_restack",
            [
                "git",
                "commit",
                "-am",
                f"chore({config.restack_commit_scope}): restack PR #{next_pr.number}",
            ],
        ),
        (
            "push_branch",
            [
                "git",
                "push",
                "--force-with-lease" if config.push_force_with_lease else "--force",
                "origin",
                branch_name,
            ],
        ),
    ]

    for step_name, cmd in steps:
        step = _run_git_step(step_name, cmd, repo_dir)
        execution.steps.append(step)
        if not step.success:
            execution.success = False
            execution.error = f"Step failed: {step_name}"
            return execution
        if step_name == "push_branch":
            if github_client is None:
                execution.success = False
                execution.error = "Cannot wait for CI without GitHub client"
                execution.ci_wait_state = "failure"
                execution.ci_wait_reason = execution.error
                return execution
            ci_state, ci_reason = wait_for_ci_green(
                next_pr,
                github_client,
                timeout=config.ci_wait_timeout_seconds,
                poll_interval=config.ci_poll_interval_seconds,
            )
            execution.ci_wait_state = ci_state
            execution.ci_wait_reason = ci_reason
            if ci_state != "success":
                execution.success = False
                execution.error = ci_reason
                return execution

    execution.success = True
    return execution
