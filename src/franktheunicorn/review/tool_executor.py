"""
Execution backend for CLI review tools.

CLI review tools (CodeRabbit, Claude CLI, Snowflake review) all need a
working directory containing the project's git checkout at the PR's head
commit. Locally that's the worker's clone in ``data/repos/<owner>/<repo>``.
Remotely, we SSH to a host, clone (or fetch) the repo there, and run the
CLI on the remote. The two execution modes share a small interface so the
tool wrappers don't have to know which one they're using.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from franktheunicorn.config.models import RemoteExecutionConfig

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120


@dataclass
class ExecResult:
    """Subset of ``subprocess.CompletedProcess`` we actually use.

    Decoupling from ``CompletedProcess`` lets ``RemoteSSHExecutor`` return
    a uniform shape even when the underlying SSH layer fails before the
    remote command runs.
    """

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ToolExecutor(Protocol):
    """Run a CLI command in a working directory containing a repo checkout."""

    def prepare_repo(
        self,
        owner: str,
        repo: str,
        local_path: Path | None = None,
        clone_url: str = "",
    ) -> str | None:
        """Ensure a checkout exists and return its working-directory path.

        For ``LocalExecutor`` this is a no-op that just validates
        ``local_path``. For ``RemoteSSHExecutor`` this clones (or fetches)
        the repo onto the remote host. Returns ``None`` on failure.
        """

    def run(
        self,
        cmd: list[str],
        cwd: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        stdin: str | None = None,
    ) -> ExecResult | None:
        """Run ``cmd`` in ``cwd``. Returns ``None`` on infrastructure failure.

        ``cmd`` arguments are passed verbatim — callers should not
        pre-quote them. ``stdin`` is fed to the process as text input.
        """


@dataclass
class LocalExecutor:
    """Run commands in a local subprocess."""

    def prepare_repo(
        self,
        owner: str,
        repo: str,
        local_path: Path | None = None,
        clone_url: str = "",
    ) -> str | None:
        if local_path is None:
            logger.debug("LocalExecutor: no local_path provided for %s/%s", owner, repo)
            return None
        if not local_path.exists():
            logger.debug("LocalExecutor: local_path missing for %s/%s: %s", owner, repo, local_path)
            return None
        return str(local_path)

    def run(
        self,
        cmd: list[str],
        cwd: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        stdin: str | None = None,
    ) -> ExecResult | None:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout,
                input=stdin,
            )
        except FileNotFoundError:
            logger.warning("CLI not found on PATH: %s", cmd[0] if cmd else "(empty)")
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                "CLI timed out after %ds: %s",
                timeout,
                cmd[0] if cmd else "(empty)",
            )
            return None
        return ExecResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )


@dataclass
class RemoteSSHExecutor:
    """Run commands on a remote host over SSH.

    The remote workspace is laid out as
    ``<remote_workspace_dir>/<owner>/<repo>``, one git clone per project.
    ``prepare_repo`` is idempotent: it clones if missing, fetches if
    present.
    """

    config: RemoteExecutionConfig

    def _ssh_target(self) -> str:
        if self.config.user:
            return f"{self.config.user}@{self.config.host}"
        return self.config.host

    def _ssh_command(self) -> list[str]:
        cmd = [*self.config.ssh_command, "-o", "BatchMode=yes"]
        if self.config.port:
            cmd += ["-p", str(self.config.port)]
        if self.config.ssh_key_path:
            cmd += ["-i", self.config.ssh_key_path]
        cmd += list(self.config.ssh_extra_args)
        cmd.append(self._ssh_target())
        return cmd

    def _remote_repo_path(self, owner: str, repo: str) -> str:
        base = self.config.remote_workspace_dir.rstrip("/")
        return f"{base}/{owner}/{repo}"

    @staticmethod
    def _quote_remote_path(path: str) -> str:
        """Shell-quote a remote path while letting a leading ``~`` expand.

        ``shlex.quote`` wraps tilde-prefixed paths in single quotes, which
        blocks the remote shell from expanding ``~`` to ``$HOME`` — so a
        default ``~/.frank-remote`` would be cloned under a literal ``~``
        directory. Rewriting to ``$HOME/...`` and emitting the prefix
        inside double quotes lets the shell expand it while the suffix
        stays safely quoted (adjacent quoted strings concatenate).
        """
        if path == "~":
            return '"$HOME"'
        if path.startswith("~/"):
            suffix = path[1:]  # leading "/..."
            return '"$HOME"' + shlex.quote(suffix)
        return shlex.quote(path)

    @staticmethod
    def _https_fallback_url(url: str) -> str:
        """Derive an HTTPS clone URL from a git+ssh URL, or return empty string.

        Returns empty string when the URL is already HTTPS or doesn't look like
        a git@host:owner/repo.git SSH URL (no fallback needed).
        """
        m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", url)
        if not m:
            return ""
        host, path = m.group(1), m.group(2)
        return f"https://{host}/{path}.git"

    def prepare_repo(
        self,
        owner: str,
        repo: str,
        local_path: Path | None = None,
        clone_url: str = "",
    ) -> str | None:
        if not clone_url:
            clone_url = self.config.clone_url_template.format(owner=owner, repo=repo)

        https_fallback = self._https_fallback_url(clone_url)

        remote_dir = self._remote_repo_path(owner, repo)
        parent_dir = f"{self.config.remote_workspace_dir.rstrip('/')}/{owner}"

        quoted_parent = self._quote_remote_path(parent_dir)
        quoted_remote = self._quote_remote_path(remote_dir)

        # Idempotent: clone-or-fetch in a single round trip. Fetches all
        # branches so the tool can resolve origin/main, origin/master, etc.
        # Emits "op=clone" or "op=fetch" to stdout so callers can distinguish
        # which operation failed when the exit code is non-zero.
        # When the primary clone URL is git+ssh, we fall back to HTTPS so a
        # missing SSH deploy key on the remote doesn't permanently block cloning.
        if https_fallback:
            clone_cmd = (
                f"git clone --quiet {shlex.quote(clone_url)} {quoted_remote} "
                f"|| git clone --quiet {shlex.quote(https_fallback)} {quoted_remote}"
            )
        else:
            clone_cmd = f"git clone --quiet {shlex.quote(clone_url)} {quoted_remote}"

        script = (
            f"set -e; "
            f"mkdir -p {quoted_parent}; "
            f"if [ -d {quoted_remote}/.git ]; then "
            f"echo 'op=fetch'; "
            f"cd {quoted_remote} && git fetch --quiet --all --prune; "
            f"else "
            f"echo 'op=clone'; "
            f"{clone_cmd}; "
            f"fi"
        )

        backoff_delays = (5, 15, 60, 300)
        cumulative_sleep = 0
        op_name = "clone/fetch"
        result = None
        ssh_argv = [*self._ssh_command(), script]
        for attempt, _sentinel in enumerate((*backoff_delays, None)):
            try:
                result = subprocess.run(
                    ssh_argv,
                    capture_output=True,
                    text=True,
                    timeout=self.config.prepare_timeout_seconds,
                )
            except FileNotFoundError:
                logger.warning(
                    "SSH connection error: binary %r not on PATH; remote execution unavailable",
                    self.config.ssh_command[0],
                )
                return None
            except subprocess.TimeoutExpired:
                logger.warning(
                    "SSH connection to %s timed out after %ds while preparing %s/%s",
                    self.config.host,
                    self.config.prepare_timeout_seconds,
                    owner,
                    repo,
                )
                return None

            op_name = "clone" if "op=clone" in (result.stdout or "") else "fetch"

            if result.returncode == 0:
                return remote_dir

            # SSH itself failed (connection refused, unreachable, auth error)
            # when exit code is 255; anything else is a remote command failure.
            if result.returncode == 255:
                error_kind = "SSH connection error"
            else:
                error_kind = "remote command error"

            logger.debug(
                "Remote git %s failed for %s/%s on %s (%s, rc=%d); command: %s; stderr: %s",
                op_name,
                owner,
                repo,
                self.config.host,
                error_kind,
                result.returncode,
                " ".join(ssh_argv),
                (result.stderr or "")[:300],
            )

            if _sentinel is None:
                break
            delay = _sentinel
            cumulative_sleep += delay
            if delay >= 60:
                logger.warning(
                    "Backing off %ds after remote git %s %s for %s/%s on %s (attempt %d/%d)",
                    delay,
                    op_name,
                    error_kind,
                    owner,
                    repo,
                    self.config.host,
                    attempt + 1,
                    len(backoff_delays),
                )
            else:
                logger.debug(
                    "Retrying remote git %s for %s/%s after %ds (attempt %d/%d) ...",
                    op_name,
                    owner,
                    repo,
                    delay,
                    attempt + 1,
                    len(backoff_delays),
                )
            time.sleep(delay)

        logger.warning(
            "Remote git %s failed for %s/%s on %s after %d attempts: %s",
            op_name,
            owner,
            repo,
            self.config.host,
            len(backoff_delays) + 1,
            (result.stderr or "")[:300] if result is not None else "",
        )
        return None

    def run(
        self,
        cmd: list[str],
        cwd: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        stdin: str | None = None,
    ) -> ExecResult | None:
        # Build a remote shell command: cd + the quoted argv. We quote
        # every argument so paths with spaces or shell metacharacters in
        # ``cmd`` survive the trip through ssh's remote shell. ``cwd`` is
        # whatever ``prepare_repo`` returned — typically a path under
        # ``remote_workspace_dir``, which may start with ``~`` and needs
        # the same expansion-aware quoting as ``prepare_repo``.
        quoted_cmd = " ".join(shlex.quote(part) for part in cmd)
        remote_invocation = f"cd {self._quote_remote_path(cwd)} && {quoted_cmd}"

        try:
            result = subprocess.run(
                [*self._ssh_command(), remote_invocation],
                capture_output=True,
                text=True,
                timeout=timeout,
                input=stdin,
            )
        except FileNotFoundError:
            logger.warning(
                "ssh binary %r not on PATH; remote execution unavailable",
                self.config.ssh_command[0],
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                "Remote command timed out after %ds: %s",
                timeout,
                cmd[0] if cmd else "(empty)",
            )
            return None
        return ExecResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )


def make_executor(remote: RemoteExecutionConfig | None) -> ToolExecutor:
    """Pick an executor based on a tool's ``remote`` config block."""
    if remote is None or remote.mode == "local":
        return LocalExecutor()
    if remote.mode == "ssh":
        return RemoteSSHExecutor(config=remote)
    # The Pydantic validator already rejects unknown modes; this is
    # belt-and-suspenders against future enum drift.
    logger.warning("Unknown remote.mode %r; falling back to local execution.", remote.mode)
    return LocalExecutor()
