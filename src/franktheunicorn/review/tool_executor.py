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


def _git_verbosity_flag(attempt: int) -> str:
    """Return the git verbosity flag appropriate for a given retry attempt.

    Escalates from quiet → normal → verbose so early attempts stay terse but
    later retries surface diagnostic output.
    """
    if attempt >= 3:
        return "--verbose"
    if attempt >= 1:
        return ""
    return "--quiet"


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

    def _probe_ssh(self) -> bool:
        """Run ``ssh … true`` to test bare SSH connectivity, independent of git.

        Returns True when the connection succeeds, False on any failure.
        A short connect-timeout (10 s) keeps this non-blocking.
        """
        try:
            probe = subprocess.run(
                [*self._ssh_command(), "-o", "ConnectTimeout=10", "true"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return probe.returncode == 0

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

    @staticmethod
    def _ssh_fallback_url(url: str) -> str:
        """Derive a git+ssh URL from an HTTPS clone URL, or return empty string.

        Returns empty string when the URL is already git+ssh or doesn't look like
        an https://host/owner/repo.git URL (no fallback needed).
        """
        m = re.match(r"^https://([^/]+)/(.+?)(?:\.git)?$", url)
        if not m:
            return ""
        host, path = m.group(1), m.group(2)
        return f"git@{host}:{path}.git"

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
        ssh_fallback = self._ssh_fallback_url(clone_url)

        remote_dir = self._remote_repo_path(owner, repo)
        parent_dir = f"{self.config.remote_workspace_dir.rstrip('/')}/{owner}"

        quoted_parent = self._quote_remote_path(parent_dir)
        quoted_remote = self._quote_remote_path(remote_dir)

        backoff_delays = (5, 15, 60, 300)
        cumulative_sleep = 0
        op_name = "clone/fetch"
        result = None
        all_ssh_unreachable = True
        ssh_argv: list[str] = []
        for attempt, _sentinel in enumerate((*backoff_delays, None)):
            # Build the script per-attempt so git verbosity can escalate:
            # attempt 0 → --quiet, attempts 1-2 → (no flag), attempt 3+ → --verbose
            git_flag = _git_verbosity_flag(attempt)
            git_flag_str = f" {git_flag}" if git_flag else ""

            # Idempotent clone-or-fetch. Emits "op=clone"/"op=fetch" to stdout.
            # Fetch tries the primary remote first, then falls back to the
            # alternate protocol URL (SSH→HTTPS or HTTPS→SSH) so a dead origin
            # doesn't permanently block work.
            if https_fallback:
                # Primary URL is git+ssh; fall back to HTTPS on both paths.
                clone_cmd = (
                    f"git clone{git_flag_str} {shlex.quote(clone_url)} {quoted_remote} "
                    f"|| git clone{git_flag_str} {shlex.quote(https_fallback)} {quoted_remote}"
                )
                fetch_cmd = (
                    f"git fetch{git_flag_str} --all --prune "
                    f"|| git fetch{git_flag_str} {shlex.quote(https_fallback)} --update-head-ok"
                )
            elif ssh_fallback:
                # Primary URL is HTTPS; fall back to SSH on the fetch path.
                # Clone keeps HTTPS-only (SSH key may not be configured for clone).
                clone_cmd = f"git clone{git_flag_str} {shlex.quote(clone_url)} {quoted_remote}"
                fetch_cmd = (
                    f"git fetch{git_flag_str} --all --prune "
                    f"|| git fetch{git_flag_str} {shlex.quote(ssh_fallback)} --update-head-ok"
                )
            else:
                clone_cmd = f"git clone{git_flag_str} {shlex.quote(clone_url)} {quoted_remote}"
                fetch_cmd = f"git fetch{git_flag_str} --all --prune"

            script = (
                f"set -e; "
                f"mkdir -p {quoted_parent}; "
                f"if [ -d {quoted_remote}/.git ]; then "
                f"echo 'op=fetch'; "
                f"cd {quoted_remote} && {fetch_cmd}; "
                f"else "
                f"echo 'op=clone'; "
                f"{clone_cmd}; "
                f"fi"
            )
            ssh_argv = [*self._ssh_command(), script]
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
                # After the first retry has also failed with rc=255, run a bare
                # `ssh … true` probe to confirm transport is down and emit a
                # clear diagnostic before committing to the long backoff.
                if attempt == 1 and not self._probe_ssh():
                    port_hint = f" port {self.config.port}" if self.config.port else ""
                    logger.warning(
                        "SSH transport to %s%s is down (bare connectivity probe failed)"
                        " — git operations for %s/%s will keep retrying but are unlikely"
                        " to succeed until the host is reachable",
                        self.config.host,
                        port_hint,
                        owner,
                        repo,
                    )
            else:
                error_kind = "remote command error"
                all_ssh_unreachable = False

            cmd_str = " ".join(ssh_argv)
            stdout_snippet = (result.stdout or "")[:300]
            stderr_snippet = (result.stderr or "")[:300]

            if _sentinel is None:
                break
            delay = _sentinel
            cumulative_sleep += delay
            if delay >= 60:
                logger.warning(
                    "Backing off %ds after remote git %s %s for %s/%s on %s (attempt %d/%d)"
                    " — cmd: %s; stdout: %s; stderr: %s",
                    delay,
                    op_name,
                    error_kind,
                    owner,
                    repo,
                    self.config.host,
                    attempt + 1,
                    len(backoff_delays),
                    cmd_str,
                    stdout_snippet or "(empty)",
                    stderr_snippet or "(empty)",
                )
            else:
                logger.debug(
                    "Remote git %s failed for %s/%s on %s (%s, rc=%d); retrying in %ds"
                    " (attempt %d/%d) — cmd: %s; stdout: %s; stderr: %s",
                    op_name,
                    owner,
                    repo,
                    self.config.host,
                    error_kind,
                    result.returncode,
                    delay,
                    attempt + 1,
                    len(backoff_delays),
                    cmd_str,
                    stdout_snippet or "(empty)",
                    stderr_snippet or "(empty)",
                )
            time.sleep(delay)

        if all_ssh_unreachable:
            port_hint = f" port {self.config.port}" if self.config.port else ""
            logger.warning(
                "Remote git %s failed for %s/%s: SSH host %s%s was unreachable after %d"
                " attempts — check SSH connectivity — cmd: %s; stderr: %s",
                op_name,
                owner,
                repo,
                self.config.host,
                port_hint,
                len(backoff_delays) + 1,
                " ".join(ssh_argv),
                (result.stderr or "")[:300] if result is not None else "(no result)",
            )
        else:
            logger.warning(
                "Remote git %s failed for %s/%s on %s after %d attempts"
                " — cmd: %s; stdout: %s; stderr: %s",
                op_name,
                owner,
                repo,
                self.config.host,
                len(backoff_delays) + 1,
                " ".join(ssh_argv),
                (result.stdout or "")[:300] if result is not None else "(no result)",
                (result.stderr or "")[:300] if result is not None else "(no result)",
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
