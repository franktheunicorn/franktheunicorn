"""
Differential test verification with Docker containers (§9).

Runs scoped tests on both the PR branch and the base branch with
cherry-picked test files. Compares results to produce a differential
verdict: GOOD, SUSPECT, BROKEN, or INFRA.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

from franktheunicorn.core.models import TestRun
from franktheunicorn.worker.test_identifier import identify_test_scope
from franktheunicorn.worker.test_image import resolve_image
from franktheunicorn.worker.test_workspace import (
    _ensure_ref,
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


#: Touched in $HOME when build_command fails, so a failed build reads as infra.
_BUILD_FAILED_MARKER = ".frank-build-failed"
_HOME_MOUNT = "/frank-home"
_CACHE_MOUNT = "/frank-cache"
_WARM_RETRY_SECONDS = 6 * 3600


def _build_cache_dir(project_config: ProjectConfig) -> Path:
    from django.conf import settings

    path = Path(settings.DATA_DIR) / "test-cache" / f"{project_config.owner}-{project_config.repo}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _warm_recently_failed(project_config: ProjectConfig) -> bool:
    cutoff = time.time() - _WARM_RETRY_SECONDS
    return any(
        f.stat().st_mtime > cutoff
        for f in _build_cache_dir(project_config).glob(".frank-warm-failed-*")
    )


def _is_rootless(docker: Any) -> bool:
    try:
        return any("rootless" in o for o in docker.info().get("SecurityOptions") or [])
    except Exception:
        return False


def _merge_base(repo_path: Path, base_sha: str, head_sha: str) -> str:
    proc = subprocess.run(
        ["git", "merge-base", base_sha, head_sha],
        capture_output=True,
        text=True,
        cwd=str(repo_path),
        timeout=60,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _warm_key(workspace: Path, key_files: list[str]) -> str:
    """Hash of the build-defining files, so the cache re-warms when they change."""
    h = hashlib.sha256()
    for rel in key_files:
        root = workspace / rel
        for f in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if f.is_file():
                h.update(str(f.relative_to(workspace)).encode())
                h.update(f.read_bytes())
    return h.hexdigest()[:16]


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

        if tests.test_globs:
            test_scope = [t for t in test_scope if any(fnmatch(t, g) for g in tests.test_globs)]
            if not test_scope:
                logger.debug("PR #%d: no identified test matches tests.test_globs", pr.number)
                return None

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

        # Before the image build and the TestRun row: during a warm backoff a
        # run would only rebuild the image and file another failure, every cycle.
        if not force and tests.build_command and _warm_recently_failed(project_config):
            logger.info(
                "PR #%d: test run skipped — the %s build-cache warm failed in the last %dh",
                pr.number,
                project_config.full_name,
                _WARM_RETRY_SECONDS // 3600,
            )
            return None

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
            if tests.build_command:
                # The fork point too: a PR branched off an older master builds
                # against that master's dependencies (Spark's snowflake-jdbc
                # thin switch broke every older PR's offline build). Still base
                # code, so still trusted; same build files is a no-op.
                # merge-base needs the head in the clone, and a new head isn't yet.
                _ensure_ref(repo_path, pr.head_sha)
                for sha in {pr.base_sha, _merge_base(repo_path, pr.base_sha, pr.head_sha)} - {""}:
                    self._warm_build_cache(docker, image, repo_path, sha, project_config)

            with pr_branch_workspace(repo_path, pr.head_sha) as pr_ws:
                pr_result = self._run_container(
                    docker,
                    image,
                    pr_ws,
                    test_scope,
                    resources,
                    tests,
                    project_config,
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
                    project_config,
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

    def _warm_build_cache(
        self,
        docker: Any,
        image: str,
        repo_path: Path,
        base_sha: str,
        project_config: ProjectConfig,
    ) -> None:
        """Fill the dependency cache by building the *base* checkout with network.

        Base code is trusted the way the image build already is; PR code only
        ever sees a read-only copy of the result. Skipped while the build files
        are unchanged since the last warm.
        """
        tests = project_config.tests
        cache = _build_cache_dir(project_config)
        # The poll cycle and a force-run can both get here; two warms into one
        # cache dir corrupt it.
        with (
            open(cache / ".frank-lock", "w") as lock,
            pr_branch_workspace(repo_path, base_sha) as ws,
        ):
            fcntl.flock(lock, fcntl.LOCK_EX)
            key = _warm_key(ws, tests.warm_key_files)
            marker = cache / f".frank-warmed-{key}"
            if marker.exists():
                return
            # A failed warm is a 90-minute build; don't redo it every cycle.
            failed = cache / f".frank-warm-failed-{key}"
            if failed.exists() and time.time() - failed.stat().st_mtime < _WARM_RETRY_SECONDS:
                msg = f"build cache warm failed for build files {key} recently; retrying later"
                raise RuntimeError(msg)
            logger.info(
                "Warming %s build cache from base %s", project_config.full_name, base_sha[:12]
            )
            result = self._run_in_container(
                docker,
                image,
                ws,
                tests.warm_command or tests.build_command or "",
                tests,
                RESOURCE_TIERS.get(tests.resource_tier, RESOURCE_TIERS["standard"]),
                home=cache,
                network=True,
                timeout=tests.build_timeout_seconds,
            )
            if result["exit_code"] != 0:
                failed.touch()
                tail = result["stderr"][-1500:] or result["stdout"][-1500:]
                msg = f"build cache warm failed (exit {result['exit_code']}): {tail}"
                raise RuntimeError(msg)
            failed.unlink(missing_ok=True)
            # Additive: a cache warmed for two sets of build files serves both.
            marker.touch()

    def _run_container(
        self,
        docker: Any,
        image: str,
        workspace: Path,
        test_files: list[str],
        resources: dict[str, Any],
        tests: TestExecutionConfig,
        project_config: ProjectConfig | None = None,
    ) -> dict[str, Any]:
        """Run tests in a Docker container.

        Returns a dict with 'exit_code', 'stdout', 'stderr', 'timed_out'.
        """
        command = tests.test_command.format(tests=" ".join(test_files))
        if tests.build_command and project_config is not None:
            # A marker file, not an exit code: a test can exit with any code,
            # and a failed build must not read as a test result.
            command = (
                f"cp -a {_CACHE_MOUNT}/. $HOME/ && ({tests.build_command}) || "
                f"{{ touch $HOME/{_BUILD_FAILED_MARKER}; exit 1; }}; {command}"
            )
            cache = _build_cache_dir(project_config)
            # Shared lock: a warm writing the cache mid-copy would hand this run
            # half a jar. The home sits beside the cache, on the data disk.
            with (
                open(cache / ".frank-lock", "w") as lock,
                tempfile.TemporaryDirectory(prefix="frank-home-", dir=cache.parent) as home,
            ):
                fcntl.flock(lock, fcntl.LOCK_SH)
                result = self._run_in_container(
                    docker,
                    image,
                    workspace,
                    command,
                    tests,
                    resources,
                    home=Path(home),
                    cache=cache,
                    timeout=resources.get("timeout", 900) + tests.build_timeout_seconds,
                )
                result["build_failed"] = (Path(home) / _BUILD_FAILED_MARKER).exists()
                return result

        return self._run_in_container(docker, image, workspace, command, tests, resources)

    def _run_in_container(
        self,
        docker: Any,
        image: str,
        workspace: Path,
        command: str,
        tests: TestExecutionConfig,
        resources: dict[str, Any],
        *,
        home: Path | None = None,
        cache: Path | None = None,
        network: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run *command* in the locked-down container (§9.5).

        With *home*, the workspace and that host dir are writable and the
        container runs as the worker's uid: a build writes target/ dirs, and on
        a rootful daemon root-owned output couldn't be cleaned up. *cache* is
        mounted read-only. *network* is only ever set for the base-checkout warm.
        """
        # Writable scratch area separate from the read-only repo mount. Tests
        # need somewhere to write pytest cache, pip cache, $HOME files, etc.
        # Mounting tmpfs at the same path as the repo bind would either fail
        # (Docker rejects duplicate destinations) or hide the repo, so keep
        # them disjoint.
        scratch = "/frank-scratch"
        env = {
            "HOME": _HOME_MOUNT if home is not None else scratch,
            "TMPDIR": scratch,
            "PYTHONUSERBASE": scratch,
            **dict(tests.env),
            **(dict(tests.offline_env) if cache is not None else {}),
        }
        volumes = {str(workspace): {"bind": tests.workdir, "mode": "ro" if home is None else "rw"}}
        extra: dict[str, Any] = {}
        passwd: Path | None = None
        if home is not None:
            volumes[str(home)] = {"bind": _HOME_MOUNT, "mode": "rw"}
            # Rootless already maps container root to us; a uid there is a subuid.
            if not _is_rootless(docker):
                uid, gid = os.getuid(), os.getgid()
                extra["user"] = f"{uid}:{gid}"
                # The image has no entry for our uid, and getpwuid() returning
                # nothing breaks the JDK's login (Hadoop: "invalid null input: name").
                fd, name = tempfile.mkstemp(prefix="frank-passwd-")
                passwd = Path(name)
                with os.fdopen(fd, "w") as fh:
                    fh.write(
                        f"root:x:0:0::/root:/bin/sh\nfrank:x:{uid}:{gid}::{_HOME_MOUNT}:/bin/sh\n"
                    )
                passwd.chmod(0o644)
                volumes[name] = {"bind": "/etc/passwd", "mode": "ro"}
        if cache is not None:
            volumes[str(cache)] = {"bind": _CACHE_MOUNT, "mode": "ro"}

        container = None
        try:
            container = docker.containers.run(
                image,
                command=["sh", "-c", command],
                detach=True,
                network_mode="bridge" if network else "none",  # §9.5: PR code gets none
                # nano_cpus, not cpu_count: cpu_count is a Windows-only
                # HostConfig option that the Linux daemon silently ignores,
                # which left the §9.4 CPU tiers unenforced.
                nano_cpus=int(resources.get("cpu_count", 4) * 1_000_000_000),
                mem_limit=resources.get("mem_limit", "8g"),
                security_opt=["no-new-privileges"],
                cap_drop=["ALL"],
                read_only=True,
                tmpfs={"/tmp": "size=1G", scratch: "size=2G,exec"},
                volumes=volumes,
                working_dir=tests.workdir,
                environment=env,
                **extra,
            )

            timeout = timeout or resources.get("timeout", 900)
            result = container.wait(timeout=timeout)
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            container.remove(force=True)
            container = None

            return {
                "exit_code": result.get("StatusCode", -1),
                # Tails: with a build step the head is all build log, and the
                # test summary is at the end.
                "stdout": stdout[-10000:],
                "stderr": stderr[-5000:],
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
            if passwd is not None:
                passwd.unlink(missing_ok=True)
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
        INFRA:   base errors on import/setup, or either build failed (inconclusive)
        """
        if pr_result.get("build_failed") or base_result.get("build_failed"):
            # No build, no test result. On the PR side often a new dependency
            # the offline cache doesn't have.
            return "infra"

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
