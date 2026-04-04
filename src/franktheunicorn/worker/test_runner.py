"""
Differential test verification with Docker containers (§9).

Runs scoped tests on both the PR branch and the base branch with
cherry-picked test files. Compares results to produce a differential
verdict: GOOD, SUSPECT, BROKEN, or INFRA.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from franktheunicorn.core.models import TestRun
from franktheunicorn.worker.test_identifier import identify_test_scope

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)

# Resource tiers (§9.4)
RESOURCE_TIERS: dict[str, dict[str, Any]] = {
    "heavy": {"cpu_count": 8, "mem_limit": "16g", "timeout": 45 * 60},
    "standard": {"cpu_count": 4, "mem_limit": "8g", "timeout": 15 * 60},
    "light": {"cpu_count": 2, "mem_limit": "4g", "timeout": 5 * 60},
}


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
    ) -> TestRun | None:
        """Run a differential test for a PR.

        Returns the TestRun record, or None if tests can't run.
        """
        test_config = getattr(project_config, "test_config", None)
        if test_config is not None and not test_config.get("enabled", False):
            return None

        # Identify test scope.
        changed_files: list[str] = pr.changed_files or []
        test_scope = identify_test_scope(changed_files, pr.body or "")

        if not test_scope and not pr.likely_ai_generated:
            # No test files changed and not AI-generated — skip.
            return None

        docker = self._get_docker()
        if docker is None:
            logger.info("Docker not available; skipping test verification for PR #%d", pr.number)
            return None

        # Determine container image and resource tier.
        container_image = "python:3.12-slim"
        resource_tier = "standard"
        if test_config:
            container_image = test_config.get("container_image", container_image)
            resource_tier = test_config.get("resource_tier", resource_tier)

        resources = RESOURCE_TIERS.get(resource_tier, RESOURCE_TIERS["standard"])

        # Create TestRun record.
        test_run = TestRun.objects.create(
            pull_request=pr,
            run_type="pr_branch",
            status="running",
            test_scope=test_scope,
            container_image=container_image,
            started_at=datetime.now(tz=UTC),
        )

        try:
            pr_result = self._run_container(
                docker,
                container_image,
                test_scope,
                resources,
                "pr_branch",
            )
            base_result = self._run_container(
                docker,
                container_image,
                test_scope,
                resources,
                "base_cherry_pick",
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
        test_files: list[str],
        resources: dict[str, Any],
        run_type: str,
    ) -> dict[str, Any]:
        """Run tests in a Docker container.

        Returns a dict with 'exit_code', 'stdout', 'stderr', 'timed_out'.
        """
        test_cmd = " ".join(test_files)
        command = f"python -m pytest {test_cmd} --tb=short -q"

        container = None
        try:
            container = docker.containers.run(
                image,
                command=f"sh -c '{command}'",
                detach=True,
                network_mode="none",  # §9.5: no network access
                cpu_count=resources.get("cpu_count", 4),
                mem_limit=resources.get("mem_limit", "8g"),
                security_opt=["no-new-privileges"],
                read_only=True,
                tmpfs={"/tmp": "size=1G"},
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
