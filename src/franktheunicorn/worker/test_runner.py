"""
Differential test verification with Docker containers (§9).

Runs scoped tests on both the PR branch and the base branch with
cherry-picked test files. Compares results to produce a differential
verdict: GOOD, SUSPECT, BROKEN, or INFRA.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from franktheunicorn.core.models import TestRun
from franktheunicorn.worker.test_identifier import identify_test_scope
from franktheunicorn.worker.test_image import resolve_image
from franktheunicorn.worker.test_workspace import (
    base_cherry_pick_workspace,
    pr_branch_workspace,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig, TestExecutionConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)

# Resource tiers (§9.4)
RESOURCE_TIERS: dict[str, dict[str, Any]] = {
    "heavy": {"cpu_count": 8, "mem_limit": "16g", "timeout": 45 * 60},
    "standard": {"cpu_count": 4, "mem_limit": "8g", "timeout": 15 * 60},
    "light": {"cpu_count": 2, "mem_limit": "4g", "timeout": 5 * 60},
}


def _is_trusted_author(pr: PullRequest, project_config: ProjectConfig) -> bool:
    """Return True if the PR author is a trusted contributor for this project.

    Trusted means: committer, frequent contributor, or the operator's own PR.
    Untrusted PRs skip automatic differential test runs; the operator can still
    trigger them manually from the dashboard.
    """
    author = (pr.author or "").lower()
    if not author:
        return False
    if pr.is_operator_pr:
        return True
    trusted = {u.lower() for u in project_config.committers + project_config.frequent_contributors}
    return author in trusted


class TestRunner:
    """Runs differential tests in Docker containers."""

    def __init__(self) -> None:
        self._docker: Any = None

    def _get_docker(self) -> Any:
        """Lazy-load Docker client. Returns None if Docker is unavailable."""
        if self._docker is not None:
            return self._docker
        try:
            import docker

            self._docker = docker.from_env()  # type: ignore[attr-defined]
            self._docker.ping()
            return self._docker
        except Exception:
            logger.debug("Docker not available", exc_info=True)
            return None

    def run_differential_test(
        self,
        pr: PullRequest,
        project_config: ProjectConfig,
        repo_path: Path | None = None,
        force: bool = False,
    ) -> TestRun | None:
        """Run a differential test for a PR.

        Returns the TestRun record, or None if tests can't run.

        When ``force=True`` (manual dashboard trigger), the trusted-author gate
        is bypassed so the operator can always run tests on any PR.
        """
        tests: TestExecutionConfig = project_config.tests
        if not tests.enabled:
            return None

        # Skip untrusted authors on automatic runs to avoid running arbitrary
        # code from unknown contributors without operator review.
        if not force and not _is_trusted_author(pr, project_config):
            logger.info(
                "Skipping automatic test run for PR #%d: author %r is not a trusted contributor",
                pr.number,
                pr.author,
            )
            return None

        # Identify test scope.
        changed_files: list[str] = pr.changed_files or []
        test_scope = identify_test_scope(changed_files, pr.body or "")

        if not test_scope and not pr.likely_ai_generated:
            # No test files changed and not AI-generated — skip.
            return None

        if repo_path is None or not repo_path.is_dir():
            logger.info(
                "Repo clone unavailable for %s; skipping test verification for PR #%d",
                project_config.full_name,
                pr.number,
            )
            return None

        if not pr.head_sha or not pr.base_sha:
            logger.info(
                "PR #%d missing base/head SHA; skipping test verification",
                pr.number,
            )
            return None

        # The poll loop calls this for every open PR every cycle — skip heads
        # we've already produced a *result* for so unchanged PRs don't burn a
        # container run (and a TestRun row) per poll. Only terminal runs that
        # completed count: an orphaned "running"/"pending" row left by a
        # killed worker must NOT block re-verification (the worker is meant to
        # be restart-safe), and a "failed" (infra-errored) run is retryable.
        # force=True (dashboard) bypasses.
        if (
            not force
            and TestRun.objects.filter(
                pull_request=pr,
                head_sha=pr.head_sha,
                status__in=("completed", "timeout"),
            ).exists()
        ):
            logger.debug(
                "PR #%d head %s already has a completed test run; skipping",
                pr.number,
                pr.head_sha[:12],
            )
            return None

        docker = self._get_docker()
        if docker is None:
            logger.info("Docker not available; skipping test verification for PR #%d", pr.number)
            return None

        resources = RESOURCE_TIERS.get(tests.resource_tier, RESOURCE_TIERS["standard"])

        # Create TestRun record. ``container_image`` is filled in once the
        # image has been resolved (build may take a while).
        test_run = TestRun.objects.create(
            pull_request=pr,
            run_type="pr_branch",
            status="running",
            head_sha=pr.head_sha,
            test_scope=test_scope,
            container_image="",
            started_at=datetime.now(tz=UTC),
        )

        try:
            # Build the test image from the *base* checkout, not the PR head:
            # image builds run outside the sandbox (network access, no caps),
            # so a PR that edits the Dockerfile or requirements files must
            # not control what executes during the build. PR code only runs
            # inside the locked-down container afterwards.
            with pr_branch_workspace(repo_path, pr.base_sha) as build_ws:
                image = resolve_image(
                    docker,
                    project_config.owner,
                    project_config.repo,
                    tests,
                    build_ws,
                )
            test_run.container_image = image

            with pr_branch_workspace(repo_path, pr.head_sha) as pr_ws:
                pr_result = self._run_container(
                    docker,
                    image,
                    pr_ws,
                    test_scope,
                    resources,
                    tests,
                )

            with base_cherry_pick_workspace(
                repo_path, pr.base_sha, pr.head_sha, test_scope
            ) as base_ws:
                base_result = self._run_container(
                    docker,
                    image,
                    base_ws,
                    test_scope,
                    resources,
                    tests,
                )

            verdict = self._compute_verdict(pr_result, base_result)

            test_run.status = "completed"
            test_run.results = {
                "pr_branch": pr_result,
                "base_cherry_pick": base_result,
            }
            test_run.differential_verdict = verdict
            test_run.finished_at = datetime.now(tz=UTC)
            test_run.save()

        except Exception as exc:
            test_run.status = "failed"
            test_run.error_log = str(exc)
            test_run.finished_at = datetime.now(tz=UTC)
            test_run.save()
            logger.exception("Test run failed for PR #%d", pr.number)

        return test_run

    def _run_container(
        self,
        docker: Any,
        image: str,
        workspace: Path,
        test_files: list[str],
        resources: dict[str, Any],
        tests: TestExecutionConfig,
    ) -> dict[str, Any]:
        """Run tests in a Docker container.

        Returns a dict with 'exit_code', 'stdout', 'stderr', 'timed_out'.
        """
        command = tests.test_command.format(tests=" ".join(test_files))

        # Writable scratch area separate from the read-only repo mount. Tests
        # need somewhere to write pytest cache, pip cache, $HOME files, etc.
        # Mounting tmpfs at the same path as the repo bind would either fail
        # (Docker rejects duplicate destinations) or hide the repo, so keep
        # them disjoint.
        scratch = "/frank-scratch"
        env = {
            "HOME": scratch,
            "TMPDIR": scratch,
            "PYTHONUSERBASE": scratch,
            **dict(tests.env),
        }

        container = None
        try:
            container = docker.containers.run(
                image,
                command=["sh", "-c", command],
                detach=True,
                network_mode="none",  # §9.5: no network access
                # nano_cpus, not cpu_count: cpu_count is a Windows-only
                # HostConfig option that the Linux daemon silently ignores,
                # which left the §9.4 CPU tiers unenforced.
                nano_cpus=int(resources.get("cpu_count", 4) * 1_000_000_000),
                mem_limit=resources.get("mem_limit", "8g"),
                security_opt=["no-new-privileges"],
                cap_drop=["ALL"],
                read_only=True,
                tmpfs={"/tmp": "size=1G", scratch: "size=2G,exec"},
                volumes={str(workspace): {"bind": tests.workdir, "mode": "ro"}},
                working_dir=tests.workdir,
                environment=env,
            )

            timeout = resources.get("timeout", 900)
            result = container.wait(timeout=timeout)
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            container.remove(force=True)
            container = None

            return {
                "exit_code": result.get("StatusCode", -1),
                "stdout": stdout[:10000],
                "stderr": stderr[:5000],
                "timed_out": False,
            }
        except Exception as exc:
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Container timed out: {exc}",
                    "timed_out": True,
                }
            raise
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    logger.warning("Failed to remove Docker container after error", exc_info=True)

    def _compute_verdict(
        self,
        pr_result: dict[str, Any],
        base_result: dict[str, Any],
    ) -> str:
        """Compute differential verdict (§9.2).

        GOOD:    pass on PR, fail on base (test validates the change)
        SUSPECT: pass on both (test doesn't catch the change)
        BROKEN:  fail on both (flaky or broken)
        INFRA:   base errors on import/setup (inconclusive)
        """
        pr_pass = pr_result.get("exit_code") == 0
        base_pass = base_result.get("exit_code") == 0

        # Check for infra issues on base (import errors, setup failures, collection errors).
        base_stderr = base_result.get("stderr", "")
        infra_patterns = (
            "No module named ",
            "ModuleNotFoundError:",
            "ImportError:",
            "E   ModuleNotFoundError",
            "E   ImportError",
            "CollectionError",
            "SetupError",
            "ERRORS during collection",
        )
        if not base_pass and any(p in base_stderr for p in infra_patterns):
            return "infra"

        if pr_pass and not base_pass:
            return "good"
        if pr_pass and base_pass:
            return "suspect"
        if not pr_pass and not base_pass:
            return "broken"

        # PR fails but base passes — regression.
        return "broken"
