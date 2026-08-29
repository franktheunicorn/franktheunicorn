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

import base64
import binascii
import logging
import re
import shlex
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Protocol

from franktheunicorn.config.models import RemoteExecutionConfig

_DELIVERY_AUTO = "auto"
_DELIVERY_ARGV = "argv"
_DELIVERY_STDIN = "stdin"

#: Round-tripped to find out whether a command sent a given way actually ran.
#: Sent as two halves the remote shell has to join, so an error message quoting
#: our own argv back cannot masquerade as output. See ``_delivery_mode``.
_DELIVERY_SENTINEL_HEAD = "__frank_delivery"
_DELIVERY_SENTINEL_TAIL = "_ok__"
_DELIVERY_SENTINEL = _DELIVERY_SENTINEL_HEAD + _DELIVERY_SENTINEL_TAIL
_DELIVERY_PROBE_TIMEOUT_SECONDS = 60

#: Markers bracketing the command in a stdin-driven session, so the wrapper's and
#: the login shell's own chatter can be cut back off the output.
_FRAME_BEGIN = "__frank_out_begin__"
_FRAME_END = "__frank_out_end__"

#: Markers bracketing the base64 payloads. The command's stdout and stderr are
#: written to files on the remote and base64'd between these, because a PTY
#: session interleaves prompts, bracketed-paste escapes and the shell's echo of
#: our own script with the output — see ``_run_via_stdin``. Base64 shares no
#: characters with any of that, so the payload survives whatever the session says.
_B64_OUT = "__frank_b64out__"
_B64_ERR = "__frank_b64err__"
_B64_END = "__frank_b64end__"
#: The command's stdout size in bytes, emitted before base64 runs. Lets an absent
#: base64(1) be told apart from a command that genuinely printed nothing — see
#: ``_unframe``.
_B64_LEN = "__frank_b64len__"

#: Longest line we will write into a pty. MAX_CANON is 4096 on Linux and a line
#: at or over it is silently discarded (and hangs the shell) rather than
#: truncated, so this leaves generous headroom. See ``_stage_invocation``.
_MAX_TTY_LINE = 2000

#: Column width for the staged-command base64. Well under _MAX_TTY_LINE, since
#: each of these is its own heredoc line through the same tty.
_B64_WRAP = 900

#: Cap on how much of a remote command's output we bring back, applied on the
#: remote with ``head -c`` where the file already is. Generous against a git diff
#: and far under what a verbose agent can emit: 10 MB of output became ~50 MB live
#: on the worker across the transcript, the span copy, the alphabet filter and the
#: decode, for text the verifier then truncates to 20,000 chars. The declared byte
#: count is the untruncated size, so a capped payload is detectable.
_MAX_REMOTE_OUTPUT_BYTES = 4 * 1024 * 1024

#: Anything a base64 payload cannot contain. Stripped before decoding, because a
#: wrapper is free to fold a long line or inject a CR into the middle of one.
_NON_B64 = re.compile(r"[^A-Za-z0-9+/=]")

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120


def _decode_b64_payload(stdout: str, head: str, tail: str) -> str | None:
    """Pull one base64 payload out of a PTY session transcript and decode it.

    ``None`` when there isn't one to find, or when what's there doesn't decode —
    both mean "fall back", and neither should raise: this runs on whatever a
    remote shell happened to print.

    ``rfind`` for the head for the same reason the frame markers use it, plus the
    script assembles these markers from two shell variables so the echo of the
    script line can't contain a whole one. The payload is stripped of everything
    outside the base64 alphabet before decoding, because a wrapper is free to
    inject a carriage return or a line-wrap into the middle of a long line and
    several do.
    """
    at_head = stdout.rfind(head)
    if at_head < 0:
        return None
    start = at_head + len(head)
    at_tail = stdout.find(tail, start)
    if at_tail < 0:
        return None
    payload = _NON_B64.sub("", stdout[start:at_tail])
    if not payload:
        # An empty payload is a real answer: the command printed nothing. Only
        # distinguishable from "no payload" because the markers were both found.
        return ""
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None


def _declared_length(stdout: str, head: str, tail: str) -> int | None:
    """The byte count the remote reported for its stdout, or None if it didn't.

    Separate from the payload so "the encoder failed" and "there was nothing to
    encode" stop being the same observation.
    """
    at_head = stdout.rfind(head)
    if at_head < 0:
        return None
    start = at_head + len(head)
    at_tail = stdout.find(tail, start)
    if at_tail < 0:
        return None
    digits = re.sub(r"\D", "", stdout[start:at_tail])
    try:
        return int(digits) if digits else None
    except ValueError:  # pragma: no cover - re guarantees digits only
        return None


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


#: Phrases a coding-agent CLI uses when it is refusing because nobody has vouched
#: for the working directory. Matched case-insensitively against stderr and stdout
#: together.
#:
#: Worth detecting rather than reporting as a generic non-zero exit, because it is
#: both the most likely first-run failure on this path and the one whose raw output
#: is least self-explanatory in a log. Every directory this codebase drives an agent
#: in is one it created — the review clone, the verifier's ``security-verify`` tree
#: — so the first invocation in each is in a workspace the CLI has never been told
#: to trust. ``cursor-agent`` exits 1 with empty stdout and advises you to "run
#: 'agent' interactively to decide", which a worker cannot do; without this, the
#: verifier records an unexplained ``unclear`` per branch and the review path logs a
#: bare "exited with code 1".
_TRUST_REFUSAL_MARKERS = (
    "workspace trust required",
    "do you trust the contents of this directory",
    "do you trust the files in this folder",
    "trust the current workspace",
    "--trust",
)


def looks_like_workspace_trust_refusal(*streams: str) -> bool:
    """Whether a CLI's output is it refusing to act in an untrusted directory.

    Deliberately a phrase match rather than an exit-code check: the exit code is 1,
    which is also every other kind of failure. Over-matching costs a sentence of
    advice appended to an error the operator was going to read anyway, so the
    markers are kept broad — including a bare ``--trust``, on the grounds that a
    CLI mentioning that flag while failing is telling us something.
    """
    haystack = " ".join(stream or "" for stream in streams).lower()
    return any(marker in haystack for marker in _TRUST_REFUSAL_MARKERS)


def workspace_trust_advice(reviewer_name: str) -> str:
    """One sentence naming the fix, for appending to whatever error we log."""
    return (
        f"This looks like {reviewer_name} refusing to run in a directory nothing has "
        "marked as trusted — which every checkout frank creates is, on its first run. "
        f"Add the CLI's own trust flag to the {reviewer_name} entry's `trust_args` in "
        'operator.yaml (cursor-agent: `trust_args: ["--trust"]`). Note `trust_args`, '
        "not `extra_args`: the seeded entries already set it, and overriding "
        "`extra_args` replaces the seed rather than merging with it."
    )


class ToolExecutor(Protocol):
    """Run a CLI command in a working directory containing a repo checkout."""

    def prepare_repo(
        self,
        owner: str,
        repo: str,
        local_path: Path | None = None,
        clone_url: str = "",
        workspace_subdir: str = "",
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


#: Which concurrent user of the checkouts we are. Set by the worker's interactive
#: thread so a force-run's ``git checkout --detach`` cannot land in the tree the
#: poll cycle is reading a diff out of — the same class of bug the mid-cycle drain
#: caused before ``_isolated_worktree`` existed, except now genuinely simultaneous
#: rather than merely interleaved.
#:
#: A ContextVar rather than a global: it is per-thread by construction, which is
#: exactly the scope of the problem.
_workspace_lane: ContextVar[str] = ContextVar("frank_workspace_lane", default="")


def set_workspace_lane(name: str) -> None:
    """Name this thread's checkout lane. Call once, at the top of the thread."""
    _workspace_lane.set(name)


@contextmanager
def workspace_lane(name: str) -> Iterator[None]:
    """Run the body in checkout lane *name*, restoring the previous lane after.

    Scoped rather than sticky because a ContextVar set in the main thread outlives
    the call: setting it unscoped from a test leaked the lane into every later test
    in the session, which moved eleven unrelated checkouts into ``force-run/``.
    """
    token = _workspace_lane.set(name)
    try:
        yield
    finally:
        _workspace_lane.reset(token)


def lane_subdir(workspace_subdir: str) -> str:
    """*workspace_subdir* widened to this thread's lane.

    Returns the lane itself when the caller wanted no isolation at all: the review
    pipeline reads the main clone directly, and that is precisely the tree a
    concurrent force-run must not be checking branches out of.
    """
    lane = _workspace_lane.get()
    if not lane:
        return workspace_subdir
    return f"{workspace_subdir}-{lane}" if workspace_subdir else lane


@dataclass
class LocalExecutor:
    """Run commands in a local subprocess."""

    def prepare_repo(
        self,
        owner: str,
        repo: str,
        local_path: Path | None = None,
        clone_url: str = "",
        workspace_subdir: str = "",
    ) -> str | None:
        if local_path is None:
            logger.debug("LocalExecutor: no local_path provided for %s/%s", owner, repo)
            return None
        if not local_path.exists():
            logger.debug("LocalExecutor: local_path missing for %s/%s: %s", owner, repo, local_path)
            return None
        subdir = lane_subdir(workspace_subdir)
        if subdir:
            return self._isolated_worktree(local_path, subdir, owner, repo)
        return str(local_path)

    def _isolated_worktree(
        self, local_path: Path, workspace_subdir: str, owner: str, repo: str
    ) -> str | None:
        """A checkout of ``local_path`` that a caller may mutate freely, or None.

        Honouring ``workspace_subdir`` here is a correctness requirement, not a
        nicety. It was accepted and ignored, so the security verifier — which asks
        for an isolated tree precisely because it runs ``git checkout --detach
        --force`` onto arbitrary release branches — was handed the review
        pipeline's own clone. Every verification left that tree detached on the
        last branch it looked at, and because commands drain *mid-cycle*, the rest
        of that poll cycle read blame and copy-pasta context from ``branch-3.5``
        while reviewing PRs against ``master``. Wrong findings on unrelated PRs,
        silently, plus ``--force`` discarding whatever was in the working tree.

        A linked worktree rather than a second clone: it shares the object store,
        so it costs a checkout rather than a fetch of something Spark-sized, and
        git keeps the two trees' HEADs genuinely independent.

        **Validated, not assumed.** Testing ``(target / ".git").exists()`` and
        returning was wrong in two ways that both end in a permanently dead tree:

        * A worktree whose admin directory has been pruned — ``git gc --auto`` runs
          ``worktree prune``, and any window where the directory was unreachable
          triggers it — still has its ``.git`` file, so the fast path handed it
          back and every git command inside answered ``fatal: not a git
          repository: (null)``. Every verification then failed, forever.
        * A worktree's ``.git`` records an **absolute** ``gitdir:``, and this
          project is documented to run the same ``data/`` tree at two prefixes
          (``docker compose`` at ``/app`` with ``./data`` bind-mounted, and host
          ``make worker``). A worktree created in the container is dead on the
          host and vice versa.

        So the check is "does git actually work in there", and a tree that fails it
        is removed and rebuilt. ``prune`` before ``add`` because git refuses to
        register a path it still has a stale record for, and the directory is
        removed first because ``add`` refuses a non-empty target.
        """
        # Sibling of the repos root, not of the owner directory. `local_path.parent`
        # is `<repos>/<owner>`, so that nested the isolation root *inside* the owner
        # namespace: `<repos>/apache/security-verify/apache/spark`, which both
        # diverges from the remote layout (`<workspace>/security-verify/...`) and
        # collides head-on with a project literally named `apache/security-verify`.
        target = local_path.parent.parent / workspace_subdir / owner / repo
        if self._worktree_is_usable(target):
            return str(target)

        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        for argv in (
            ["git", "worktree", "prune"],
            ["git", "worktree", "add", "--detach", "--force", str(target), "HEAD"],
        ):
            try:
                done = subprocess.run(
                    argv,
                    cwd=str(local_path),
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                # This function's contract — and prepare_repo's docstring — is
                # "returns None on failure". A checkout of something Spark-sized on
                # a cold cache can outrun any timeout, and letting that escape gave
                # the operator a traceback and a failed WorkerCommand instead of the
                # warning that names the setting.
                logger.warning(
                    "Could not create an isolated worktree at %s for %s/%s (%s: %s)",
                    target,
                    owner,
                    repo,
                    type(exc).__name__,
                    exc,
                )
                return None
            if done.returncode != 0 and argv[2] == "add":
                # WARNING, not debug: the caller asked for isolation and is not
                # getting it, and the alternative to saying so is handing back the
                # shared clone for something to detach.
                logger.warning(
                    "Could not create an isolated worktree at %s for %s/%s (%s). "
                    "Refusing to hand back the shared checkout — work needing "
                    "isolation will be skipped rather than corrupt it.",
                    target,
                    owner,
                    repo,
                    (done.stderr or done.stdout).strip()[:300],
                )
                return None
        return str(target)

    @staticmethod
    def _worktree_is_usable(target: Path) -> bool:
        """Whether git commands actually work inside *target*.

        The question the ``.git``-exists check was standing in for. A pruned or
        relocated worktree keeps its ``.git`` file and fails every command, so
        presence proves nothing.
        """
        if not (target / ".git").exists():
            return False
        try:
            done = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if done.returncode == 0:
            return True
        logger.info(
            "Rebuilding the isolated worktree at %s: git does not work there (%s)",
            target,
            (done.stderr or done.stdout).strip()[:200],
        )
        return False

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

    def wraps_openssh(self) -> bool:
        """Whether ``ssh_command`` is an actual OpenSSH client.

        ``ssh_command`` is documented as "however you reach the host", and people
        use that: ``["sf", "workspace", "ssh"]``, ``["gcloud", "compute", "ssh"]``,
        ``["tailscale", "ssh"]``, a shell wrapper of their own. Those are
        subcommands with their own flag grammar, and we were splicing OpenSSH
        options into them regardless — ``sf workspace ssh -o BatchMode=yes 'cd …'``,
        which ``sf`` has no reason to accept. Every remote call then failed at the
        wrapper, before SSH was even attempted.

        Decided on argv[0]'s basename so ``/usr/bin/ssh`` and a bare ``ssh``
        both count and nothing else does.
        """
        first = self.config.ssh_command[0] if self.config.ssh_command else ""
        return PurePath(first).name in ("ssh", "ssh.exe")

    def _ssh_command(self) -> list[str]:
        cmd = list(self.config.ssh_command)
        if self.wraps_openssh():
            # BatchMode stops ssh sitting on a password prompt forever. A wrapper
            # gets no equivalent — it may well prompt — so the subprocess timeout
            # in run()/_probe_ssh is what bounds those.
            cmd += ["-o", "BatchMode=yes"]
            if self.config.port:
                cmd += ["-p", str(self.config.port)]
            if self.config.ssh_key_path:
                cmd += ["-i", self.config.ssh_key_path]
        elif self.config.port or self.config.ssh_key_path:
            # Say so rather than dropping them silently: the operator wrote them
            # down and they are not being sent.
            logger.warning(
                "remote.port/ssh_key_path are OpenSSH options and %r is not ssh; "
                "ignoring them. Put wrapper-specific flags in ssh_extra_args.",
                " ".join(self.config.ssh_command),
            )
        # Always appended — this is the operator's explicit escape hatch for
        # whatever flags their wrapper does take.
        cmd += list(self.config.ssh_extra_args)
        target = self._ssh_target()
        if target:
            cmd.append(target)
        return cmd

    def _probe_ssh(self) -> bool:
        """Run ``true`` on the remote to test connectivity, independent of git.

        Returns True when the connection succeeds, False on any failure.

        Goes through ``run_script`` — the same delivery-mode detection every
        other remote call uses — rather than appending ``true`` to
        ``_ssh_command()`` directly. That used to run
        ``sf workspace ssh true``, which ``sf`` reads as "run in the workspace
        named true" and rejects, so this reported a reachable, working
        stdin-only wrapper as down. ``_check_ssh_configs`` and
        ``_check_agent_cli_reviewers`` probe the same SSH-mode reviewers at
        startup, and the mismatch between this and the agent-CLI probe's own
        (correct) sentinel round-trip was two contradictory verdicts for one
        target.

        Bounded but not tight: an uncached, non-OpenSSH config pays up to two
        15s delivery-mode probes before the real ``true`` call, so a wrapper
        that hangs rather than refusing can take ~45s to report down. Accepted
        because this runs at startup and on an already-slow retry path, not in
        a loop.
        """
        try:
            if self.wraps_openssh():
                # Real ssh: ask it directly, with ConnectTimeout. Routing this
                # through run_script cost the diagnosis — ssh's own "Connection
                # timed out" / "No route to host" on stderr, which the lines below
                # exist to log — because without ConnectTimeout the 15s subprocess
                # timeout killed it first and the operator got a generic "timed
                # out" with the cause discarded. A real ssh client also needs no
                # delivery-mode detection: it takes a trailing command by
                # definition, which is what wraps_openssh() establishes.
                result = self._spawn(
                    [*self._ssh_command(), "-o", "ConnectTimeout=10", "true"],
                    "ssh probe",
                    15,
                    None,
                )
            else:
                result = self.run_script("true", timeout=15, label="ssh probe")
        except Exception:
            logger.warning(
                "SSH probe raised for %s", " ".join(self.config.ssh_command), exc_info=True
            )
            return False
        if result is None:
            return False
        if result.returncode != 0:
            # The stderr is the whole diagnosis for a wrapper that rejected our
            # argv, and it used to be thrown away.
            logger.warning(
                "Remote probe failed (exit %d) for %s: %s",
                result.returncode,
                " ".join(self.config.ssh_command),
                (result.stderr or result.stdout or "no output").strip()[:300],
            )
        return result.returncode == 0

    def _remote_repo_path(self, owner: str, repo: str, subdir: str = "") -> str:
        base = self.config.remote_workspace_dir.rstrip("/")
        if subdir:
            return f"{base}/{subdir.strip('/')}/{owner}/{repo}"
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
        workspace_subdir: str = "",
    ) -> str | None:
        """A remote checkout for this thread's lane, or None.

        A lane is a linked *worktree* of the un-laned clone, not a second clone of
        it. Cloning per lane is minutes of network and gigabytes of disk on the
        remote for a tree whose only difference is which commit HEAD points at —
        Spark is the case that makes this obvious. A worktree shares the object
        store and git keeps the two HEADs genuinely independent, which is the
        whole requirement.

        Returns None rather than the shared tree when the worktree can't be made.
        Falling back to the shared tree would put a force-run's ``git checkout
        --detach`` back into the tree the poll cycle is reading a diff out of,
        which is the bug the lane exists to prevent; a degraded review is the
        better failure.
        """
        lane = _workspace_lane.get()
        if not lane:
            return self._clone_or_fetch(owner, repo, clone_url, workspace_subdir)

        base = self._clone_or_fetch(owner, repo, clone_url, workspace_subdir)
        if base is None:
            return None
        target = self._remote_repo_path(owner, repo, lane_subdir(workspace_subdir))
        return self._remote_worktree(base, target, lane)

    def _remote_worktree(self, base_dir: str, target_dir: str, lane: str) -> str | None:
        """Register *target_dir* as a detached worktree of *base_dir* on the remote.

        Rebuilt rather than reused: a worktree whose admin directory has been
        pruned (``git gc --auto`` runs ``worktree prune``) still looks like one
        from the outside, and every git command inside it answers ``fatal: not a
        git repository``. ``prune`` before ``add`` because git refuses a path it
        holds a stale record for, and the directory goes first because ``add``
        refuses a non-empty target. Same reasoning as the local version.
        """
        quoted_base = self._quote_remote_path(base_dir)
        quoted_target = self._quote_remote_path(target_dir)
        quoted_parent = self._quote_remote_path(target_dir.rsplit("/", 1)[0])
        script = (
            f"set -e; "
            f"mkdir -p {quoted_parent}; "
            f"git -C {quoted_base} worktree prune; "
            f"if ! git -C {quoted_target} rev-parse --git-dir >/dev/null 2>&1; then "
            f"rm -rf {quoted_target}; "
            f"git -C {quoted_base} worktree add --detach --force {quoted_target} HEAD; "
            f"fi; "
            f"git -C {quoted_target} rev-parse --git-dir >/dev/null"
        )
        result = self.run(
            ["sh", "-c", script], cwd=base_dir, timeout=self.config.prepare_timeout_seconds
        )
        if result is None or not result.ok:
            detail = "no result" if result is None else (result.stderr or result.stdout)[:300]
            logger.warning(
                "Could not make a %r worktree at %s off %s on the remote: %s. Skipping the "
                "checkout rather than sharing %s, which is what the lane is for.",
                lane,
                target_dir,
                base_dir,
                detail,
                base_dir,
            )
            return None
        logger.info("Remote %r lane ready at %s (worktree of %s)", lane, target_dir, base_dir)
        return target_dir

    def _clone_or_fetch(
        self,
        owner: str,
        repo: str,
        clone_url: str = "",
        workspace_subdir: str = "",
    ) -> str | None:
        if not clone_url:
            clone_url = self.config.clone_url_template.format(owner=owner, repo=repo)

        https_fallback = self._https_fallback_url(clone_url)
        ssh_fallback = self._ssh_fallback_url(clone_url)

        # Verbatim, not lane_subdir(): prepare_repo resolves the lane and calls this
        # for the *base* tree the lane's worktree hangs off. Applying it again here
        # would clone into the lane directory and there would be nothing to hang off.
        remote_dir = self._remote_repo_path(owner, repo, workspace_subdir)
        parent_dir = remote_dir.rsplit("/", 1)[0]

        quoted_parent = self._quote_remote_path(parent_dir)
        quoted_remote = self._quote_remote_path(remote_dir)

        backoff_delays = (5, 15, 60, 300)
        cumulative_sleep = 0
        op_name = "clone/fetch"
        result = None
        all_ssh_unreachable = True
        # Assigned each attempt from what was actually delivered. Declared-and-never-
        # written meant every failure diagnostic below logged an empty `cmd:` — the
        # one field those lines exist to supply, on the path where knowing the
        # delivered command is the whole diagnosis.
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
            # Through run_script, not a bare subprocess.run: this is the first
            # remote call the review path makes, and going straight to argv here
            # meant a stdin-only wrapper silently ignored the clone script, exited
            # 0 with no output, and had us return a path to a directory that was
            # never created — with every later step then running in it.
            # Recorded per attempt from what is actually being delivered. Declared
            # and never written, this left every failure diagnostic below logging an
            # empty `cmd:` — the one field those lines exist to supply, on the path
            # where knowing the delivered command is the whole diagnosis.
            ssh_argv = [
                *self._ssh_command(),
                f"<{self._delivery_mode(timeout=self.config.prepare_timeout_seconds)} delivery>",
            ]
            result = self.run_script(
                script,
                timeout=self.config.prepare_timeout_seconds,
                label=f"prepare {owner}/{repo}",
            )
            if result is None:
                # run_script/_spawn has already logged the specific reason
                # (binary missing, timeout, no framing).
                logger.warning(
                    "Could not reach the remote to prepare %s/%s (attempt %d)",
                    owner,
                    repo,
                    attempt + 1,
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
                    "Backing off %ds after remote git %s %s for %s/%s on %s (attempt %d/%d,"
                    " rc=%d) — cmd: %s; stdout: %s; stderr: %s",
                    delay,
                    op_name,
                    error_kind,
                    owner,
                    repo,
                    self.config.host,
                    attempt + 1,
                    len(backoff_delays),
                    result.returncode,
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
        # Build a remote shell command: cd + the quoted argv. We quote
        # every argument so paths with spaces or shell metacharacters in
        # ``cmd`` survive the trip through ssh's remote shell. ``cwd`` is
        # whatever ``prepare_repo`` returned — typically a path under
        # ``remote_workspace_dir``, which may start with ``~`` and needs
        # the same expansion-aware quoting as ``prepare_repo``.
        remote_invocation = f"cd {self._quote_remote_path(cwd)} && {quoted_cmd}"
        return self.run_script(
            remote_invocation, timeout=timeout, label=cmd[0] if cmd else "", stdin=stdin
        )

    def run_script(
        self,
        script: str,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        label: str = "",
        stdin: str | None = None,
    ) -> ExecResult | None:
        """Run an arbitrary remote shell script, honouring the delivery mode.

        Split out of :meth:`run` because ``prepare_repo`` also has a script to run
        and was calling ``subprocess.run`` directly — so it never consulted the
        delivery mode at all. That made the whole mechanism unreachable for the
        case it exists for: ``prepare_repo`` is the *first* remote call the review
        path makes, and on a stdin-only wrapper it sent the clone script as an
        argv argument, got exit 0 and no output from a wrapper that ignored it, and
        returned a path to a directory that was never created. Everything after it
        then ran in a nonexistent directory.
        """
        if self._delivery_mode(timeout=timeout) == _DELIVERY_STDIN:
            if stdin is not None:
                # The channel is already carrying the framing script, so a payload
                # would be silently dropped. Say so instead: LocalExecutor honours
                # ``stdin`` and the protocol declares it, so the two must not
                # disagree in silence.
                logger.warning(
                    "Ignoring a stdin payload for %r: this remote is driven over stdin, "
                    "which the command script already occupies.",
                    label or "(script)",
                )
            return self._run_via_stdin(script, label, timeout)
        return self._run_via_argv(script, label, timeout, stdin)

    def _run_via_argv(
        self,
        remote_invocation: str,
        label: str,
        timeout: int,
        stdin: str | None,
    ) -> ExecResult | None:
        """``ssh host 'cd … && cmd'`` — the command as a trailing argument."""
        return self._spawn([*self._ssh_command(), remote_invocation], label, timeout, stdin)

    def _stdin_delivery_runs(self, invocation: str, timeout: int) -> bool:
        """Does this wrapper run a command piped into the shell it opens?

        A capability question, and deliberately not answered through
        ``_run_via_stdin``. That path refuses when the payload cannot be
        *retrieved* — no usable ``base64`` on the remote — which is right for a
        caller that wants output and wrong for a probe that only needs to know
        whether the shell ran anything. Conflating them took a base64-less
        stdin-only remote from degraded-but-working to completely dead, and left
        the mode uncached so it re-probed twice per command forever.

        So this sends its own minimal script: the sentinel goes straight to the
        terminal with no redirect and no encoding, bracketed by nonce'd markers.
        Both are required, in order, because a hostile or confused wrapper may
        quote our command back at us — the split sentinel stops it *assembling*
        one, but a wrapper that echoes the assembled form (an error message
        naming it) would otherwise read as success. It cannot fabricate our
        nonce.
        """
        nonce = uuid.uuid4().hex[:12]
        begin, end = f"{_FRAME_BEGIN}{nonce}", f"{_FRAME_END}{nonce}"
        result = self._spawn(
            self._ssh_command(),
            "delivery probe",
            timeout,
            f"echo {begin}\n{invocation}\necho {end}\nexit\n",
        )
        if result is None:
            return False
        at_begin = result.stdout.find(begin)
        if at_begin < 0:
            return False
        at_sentinel = result.stdout.find(_DELIVERY_SENTINEL, at_begin + len(begin))
        if at_sentinel < 0:
            return False
        return result.stdout.find(end, at_sentinel) >= 0

    def _run_via_stdin(
        self,
        remote_invocation: str,
        label: str,
        timeout: int,
    ) -> ExecResult | None:
        """Pipe the command into the shell the wrapper opens.

        For a wrapper with no remote-command form. Two things make this usable
        rather than merely possible:

        * **Framing.** The wrapper and the login shell both talk on the way in
          (a "Running: ssh <ip>" line, a "Last login:" banner), and that lands in
          stdout ahead of the output. Markers around the command let us return
          exactly the command's own output, so a caller parsing ``git diff``
          doesn't get a banner prepended to the diff.
        * **Exit status.** The wrapper's exit code is its own, not the command's,
          so the real one is echoed after the end marker and read back out.

        The markers carry a per-invocation nonce. Without one, a ``git diff`` that
        happens to touch *this file* contains the literal end marker, and the body
        got cut at the diff's own text with the exit code misread as a failure.

        **The output is base64'd on the remote, not read raw between markers.**
        Markers alone got the exit code right and the *output* wrong: these
        wrappers open an interactive session on a PTY, so the shell echoes every
        line it is fed and prints a prompt before each one. Feeding the framed
        script to a real PTY-attached ``sh -i`` and running the old parser over
        the result returned, as the command's stdout::

            'sh: _direnv_hook: command not found\\r\\n\\x1b[?2004hsh-5.1$ echo
             REAL_OUTPUT_LINE_1...\\x1b[?2004l\\rREAL_OUTPUT_LINE_1\\r\\n...
             sh-5.1$ __frank_rc=$?\\r\\n...'

        — the real output, wrapped in prompts, bracketed-paste escapes, the echo
        of our own script lines and whatever the login shell's hooks had to say.
        Nothing downstream stripped any of it. The caller that hurt was
        ``claude_code_backend._parse_output``, which does ``json.loads`` and falls
        back to returning stdout verbatim: so the "model response" fed into the
        review pipeline was a shell transcript, ``is_error`` was never checked, and
        token accounting silently recorded zero.

        Base64 has no overlap with prompts, escape sequences or shell chatter, so
        the payload is unambiguous however noisy the session is. It also buys
        genuine stdout/stderr separation, which the raw form never had — both were
        interleaved on the one PTY.

        ``base64 | tr -d '\\n'`` rather than ``base64 -w0``: ``-w`` is a GNU
        extension and the BSD/macOS ``base64`` rejects it.
        """
        nonce = uuid.uuid4().hex[:12]
        begin, end = f"{_FRAME_BEGIN}{nonce}", f"{_FRAME_END}{nonce}"
        # One tail marker for both payloads: each is found by searching forward
        # from its own head, so they don't need distinct terminators.
        out_head, out_tail = f"{_B64_OUT}{nonce}", f"{_B64_END}{nonce}"
        err_head = f"{_B64_ERR}{nonce}"
        len_head = f"{_B64_LEN}{nonce}"
        stage_lines, run_body = self._stage_invocation(remote_invocation, nonce)
        # Assembled from two halves at run time so the shell's echo of these lines
        # cannot contain a complete marker — only the executed `echo` can. Same
        # trick as _DELIVERY_SENTINEL, and it means the parser doesn't have to
        # guess which occurrence is real.
        script = "\n".join(
            [
                f"__frank_h='{out_head[:-4]}'",
                f"__frank_e='{err_head[:-4]}'",
                f"__frank_t='{out_tail[:-4]}'",
                f"__frank_l='{len_head[:-4]}'",
                f"__frank_n='{nonce[-4:]}'",
                f"echo {begin}",
                # mktemp with a template. Bare `mktemp` is a usage error on
                # BSD/macOS, where it wants one — and because the `&&` chain
                # short-circuits, the second temp file was then never assigned, both
                # redirections pointed at the empty filename, and the command never
                # ran: rc=1, no stdout, no stderr, no explanation. Same portability
                # care as using `base64 | tr -d` rather than GNU-only `base64 -w0`.
                '__frank_o=$(mktemp "${TMPDIR:-/tmp}/frank.XXXXXX") && '
                '__frank_s=$(mktemp "${TMPDIR:-/tmp}/frank.XXXXXX")',
                *stage_lines,
                # A subshell, not a { } group. A group runs in the current shell, so
                # a command that ends in `exit` — or a tool that helpfully calls it —
                # terminated the session before any framing was printed, and the whole
                # run came back as "cannot be confirmed to have run at all". In a
                # subshell that exit sets $? and the framing still gets out.
                f'( {run_body} ) > "$__frank_o" 2> "$__frank_s"',
                "__frank_rc=$?",
                # The byte count, before base64 gets a chance to fail. Without it,
                # a remote with no base64(1) still printed both payload markers
                # around an empty span, so "the command printed nothing" and "the
                # encoder is missing" were the same observation — and a `git diff`
                # with no output is indistinguishable from a clean repo, which is
                # the exact failure this framing exists to prevent.
                'printf "%s%s%s%s%s\\n" "$__frank_l" "$__frank_n" '
                '"$(wc -c < "$__frank_o" | tr -d " ")" "$__frank_t" "$__frank_n"',
                # Marker, payload and marker in ONE printf, so they land contiguously.
                # Emitting them as three commands looked tidier and did not work: an
                # interactive shell prints a prompt and echoes the next line between
                # each one, and that echo contains base64-alphabet characters
                # ("base64", "printf", "tr"), so filtering the span down to the
                # base64 alphabet left the command names spliced into the payload.
                # head -c on the remote, where the file already is. Without a bound,
                # a verbose agent printing 10 MB became ~13.3 MB of base64 and about
                # 50 MB live on the worker across the transcript, the span copy, the
                # alphabet filter and the decode — for output the verifier truncates
                # to 20,000 chars anyway. The declared byte count above is the
                # *untruncated* size, so a capped payload is visible rather than
                # passing for a short one.
                f'printf "%s%s%s%s%s\\n" "$__frank_h" "$__frank_n" '
                f'"$(head -c {_MAX_REMOTE_OUTPUT_BYTES} < "$__frank_o" | base64 | tr -d "\\n")" '
                f'"$__frank_t" "$__frank_n"',
                f'printf "%s%s%s%s%s\\n" "$__frank_e" "$__frank_n" '
                f'"$(head -c {_MAX_REMOTE_OUTPUT_BYTES} < "$__frank_s" | base64 | tr -d "\\n")" '
                f'"$__frank_t" "$__frank_n"',
                # All four, not just the output pair. _stage_invocation also
                # mktemps the base64 and the decoded script, and for the verifier
                # that decoded script *is* the prompt — the reporter's raw_text and
                # POC. Leaving it behind accumulated unfixed vulnerability details
                # in a shared remote's /tmp, one pair per verification per branch.
                # ${TMPDIR:-/tmp} is 0644 by default in most images.
                'rm -f "$__frank_o" "$__frank_s" "$__frank_c" "$__frank_x"',
                f'echo "{end}:$__frank_rc"',
                "exit",
                "",
            ]
        )
        result = self._spawn(self._ssh_command(), label, timeout, script)
        if result is None:
            return None
        return self._unframe(result, label, begin, end, out_head, err_head, out_tail, len_head)

    @staticmethod
    def _stage_invocation(remote_invocation: str, nonce: str) -> tuple[list[str], str]:
        """Keep every line we feed the tty short. Returns (extra lines, what to run).

        A pty in canonical mode buffers input a line at a time and will not accept
        a line at or over ``MAX_CANON`` — 4096 bytes on Linux. Past that the
        behaviour is not "truncated with a warning", it is one of two silent
        disasters, both measured on a real pty here:

        * 4090 to 8000 bytes: the line is **discarded and the shell hangs**,
          because it never receives the newline it is waiting for. The session
          then dies on the subprocess timeout with no framing and no output.
        * ~14000 bytes: the shell runs the command on a **corrupted fragment** —
          a 14,000-byte line produced a 6,319-byte result.

        This is not a corner case for this codebase. The verifier's prompt is
        ~13.8 KB at the default ``max_report_chars``, and an agent-CLI review
        carries up to ``max_diff_chars`` = 60,000, so *every* long remote command
        over a stdin-only wrapper was landing in there.

        So: anything long is staged into a file on the remote and run with ``sh``,
        which makes the executed line a fixed ~40 bytes regardless. The transfer
        is base64 wrapped at :data:`_B64_WRAP` columns inside a quoted heredoc —
        base64 because it sidesteps every quoting question about a payload that
        contains single quotes, newlines and shell metacharacters, and a *quoted*
        heredoc because it passes bytes through without expansion. The delimiter
        contains ``_``, which is not in the base64 alphabet, so it cannot appear
        in the payload and end the heredoc early.

        Short commands are left inline: staging costs two temp files and a decode,
        and the overwhelming majority of calls here are ``git rev-parse``-sized.
        """
        if len(remote_invocation) <= _MAX_TTY_LINE:
            return [], remote_invocation

        encoded = base64.b64encode(remote_invocation.encode("utf-8")).decode("ascii")
        delimiter = f"FRANK_EOF_{nonce}"
        wrapped = [encoded[i : i + _B64_WRAP] for i in range(0, len(encoded), _B64_WRAP)]
        lines = [
            '__frank_c=$(mktemp "${TMPDIR:-/tmp}/frank.XXXXXX") && '
            '__frank_x=$(mktemp "${TMPDIR:-/tmp}/frank.XXXXXX")',
            f"cat > \"$__frank_c\" <<'{delimiter}'",
            *wrapped,
            delimiter,
            # -d and --decode both exist somewhere: GNU coreutils takes either,
            # BSD/macOS base64 wants -D on older releases and accepts -d on newer.
            'base64 -d "$__frank_c" > "$__frank_x" 2>/dev/null '
            '|| base64 -D "$__frank_c" > "$__frank_x" 2>/dev/null '
            '|| base64 --decode "$__frank_c" > "$__frank_x"',
        ]
        return lines, 'sh "$__frank_x"'

    @staticmethod
    def _unframe(
        result: ExecResult,
        label: str,
        begin: str,
        end: str,
        out_head: str = "",
        err_head: str = "",
        b64_tail: str = "",
        len_head: str = "",
    ) -> ExecResult | None:
        """Cut the command's own output and exit code out of a framed session.

        ``rfind`` for the begin marker and the *last* end marker after it, not the
        first of each. A shell attached to a PTY echoes the script it is fed, so the
        markers appear twice — once in the echo, once for real — and taking the
        first occurrence returned the script itself as the command's output.

        The exit code comes from the ``<end>:<rc>`` line; the output comes from the
        base64 payloads when they're present, because on a PTY the span between the
        markers is a session transcript rather than the command's stdout. See
        ``_run_via_stdin``. The raw-span path is kept as the fallback for a session
        that produced framing but no payload (a remote without ``base64``, most
        likely), and says so rather than pretending.
        """
        stdout = result.stdout
        at_begin = stdout.rfind(begin)
        at_end = stdout.rfind(end)
        if at_begin < 0 or at_end < at_begin:
            # The markers are the proof the command ran, so their absence means we
            # cannot say it did — which is what None means on this interface, and
            # not what the wrapper's own exit 0 means. Returning `result` verbatim
            # here is how prepare_repo reported a clone that never happened and
            # handed back a path to a directory that does not exist.
            #
            # None rather than an rc=1 ExecResult specifically because this is
            # structural, not transient: prepare_repo retries a failed result five
            # times over 380 seconds of backoff, and a wrapper that does not run
            # commands this way will not start doing so on the fourth try.
            logger.warning(
                "Remote stdin session did not echo its framing for %s, so the command "
                "cannot be confirmed to have run at all. Session said: %r",
                label or "(script)",
                (result.stdout or result.stderr or "").strip()[:200] or "nothing",
            )
            return None
        decoded_out = _decode_b64_payload(stdout, out_head, b64_tail) if out_head else None
        decoded_err = _decode_b64_payload(stdout, err_head, b64_tail) if err_head else None
        # An empty payload where the remote said it had bytes means the encoder
        # didn't run — no base64(1) on the far side. Treated as "no payload" so the
        # raw-span fallback and its warning are actually reachable: without the
        # count, both markers still printed around an empty span, so a missing
        # encoder was indistinguishable from a command that printed nothing, and
        # that reads downstream as a clean repo or an empty diff.
        declared = _declared_length(stdout, len_head, b64_tail) if len_head else None
        if (
            decoded_out
            and declared
            and declared > _MAX_REMOTE_OUTPUT_BYTES
            and len(decoded_out.encode("utf-8", errors="replace")) >= _MAX_REMOTE_OUTPUT_BYTES
        ):
            # The cap fired. Said out loud, because the whole reason the remote
            # declares its untruncated size is to make this detectable — and it was
            # computed and then never compared, so a 4 MB-plus payload came back
            # silently short. For the verifier that means the verdict JSON is cut
            # off the end and the row records "unclear", the one direction that
            # module must not fail in; per CLAUDE.md a truncated read must not look
            # like a short one.
            logger.warning(
                "Remote output for %s was %d bytes and has been truncated to %d — the "
                "tail is gone. Anything parsing this output is seeing a fragment.",
                label or "(script)",
                declared,
                _MAX_REMOTE_OUTPUT_BYTES,
            )
        if decoded_out == "" and declared:
            # None, not a fallback. The command's stdout goes to a file on the
            # remote and is only ever retrieved by base64, so with no encoder there
            # is nothing in the session text to fall back *to* — the output exists
            # and is unreachable. Returning rc=0 with empty stdout would report a
            # git diff that produced 22 bytes as a clean repo, which is the failure
            # this whole framing exists to prevent.
            logger.warning(
                "Remote said stdout was %d byte(s) for %s but the payload came back "
                "empty, so the output cannot be retrieved. The retrieval pipeline is "
                "`head -c | base64 | tr -d`, and any member of it missing or lacking "
                "these flags produces exactly this — check all three on the remote "
                "rather than base64 alone, or set remote.command_mode: argv if the "
                "wrapper accepts a trailing command.",
                declared,
                label or "(script)",
            )
            return None
        if decoded_out is None:
            body_start = stdout.find("\n", at_begin)
            body = stdout[body_start + 1 : at_end] if 0 <= body_start < at_end else ""
            if out_head:
                # Asked for a payload and didn't get one. The body below is the
                # session transcript, prompts and all, so a caller parsing it is
                # going to have a bad time — better it hears why from here than
                # discovers it as unparseable JSON three frames up.
                logger.warning(
                    "Remote stdin session for %s produced no base64 payload; falling back to "
                    "the raw session text, which on a PTY includes prompts and shell chatter. "
                    "Does the remote have base64(1)?",
                    label or "(script)",
                )
            stderr = result.stderr
        else:
            body = decoded_out
            # The remote command's own stderr, separated for the first time. The
            # wrapper's stderr is appended rather than dropped: a wrapper
            # complaint ("Workspace not found") is the whole diagnosis when the
            # remote side never ran, and it arrives on that channel.
            stderr = "\n".join(part for part in (decoded_err, result.stderr) if part)

        # "<end>:<rc>", possibly with a closing banner on the same line, so take
        # the leading integer only. Anything unparseable means we cannot claim to
        # know the exit status, and 0 would be the dangerous guess.
        tail = stdout[at_end + len(end) :]
        match = re.match(r"\s*:\s*(-?\d+)", tail)
        if match is None:
            logger.warning(
                "Remote stdin session gave no exit status for %s (%r); treating as failed.",
                label or "(script)",
                tail.split("\n", 1)[0][:80],
            )
            return ExecResult(returncode=1, stdout=body, stderr=stderr)
        return ExecResult(returncode=int(match.group(1)), stdout=body, stderr=stderr)

    def _spawn(
        self, argv: list[str], label: str, timeout: int, stdin: str | None
    ) -> ExecResult | None:
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                # errors="replace" rather than strict. A remote tool is entitled to
                # emit bytes that aren't UTF-8 — a git diff of a binary file, a
                # Latin-1 log line — and with the default strict decoding that came
                # back as an uncaught UnicodeDecodeError from inside subprocess,
                # crashing the caller instead of returning a result. Mangling a
                # byte in the middle of a diff is a far better outcome than losing
                # the whole review to a traceback.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                input=stdin,
            )
        except FileNotFoundError:
            logger.warning(
                "ssh binary %r not on PATH; remote execution unavailable",
                self.config.ssh_command[0],
            )
            return None
        except PermissionError:
            # A wrapper script that isn't chmod +x, or an ssh_command pointing at a
            # directory. Both are ordinary misconfigurations and both used to
            # escape as an uncaught PermissionError, skipping the "SSH may be
            # misconfigured" diagnostic this path exists to produce.
            logger.warning(
                "ssh command %r is not executable (a wrapper missing chmod +x, or a "
                "path to a directory); remote execution unavailable",
                self.config.ssh_command[0],
            )
            return None
        except OSError as exc:
            # Everything else the OS can refuse at exec time — E2BIG on a very long
            # argv, ENOEXEC on a script with no shebang, ENOMEM. Caught as a group
            # because the list is long, platform-dependent, and every member means
            # the same thing to a caller: the command did not run.
            logger.warning(
                "Could not run ssh command %r (%s: %s); remote execution unavailable",
                self.config.ssh_command[0],
                type(exc).__name__,
                exc,
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                "Remote command timed out after %ds: %s",
                timeout,
                label or "(script)",
            )
            return None
        return ExecResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    def _delivery_mode(self, *, timeout: int) -> str:
        """Which delivery shape this wrapper actually honours.

        Decided once per config by round-tripping a sentinel, because the naming
        gives nothing away and getting it wrong fails silently. ``sf workspace
        ssh 'cd /x && claude …'`` reads that argument as a *workspace name* and
        answers "Workspace not found"; other wrappers ignore it and open an
        interactive shell, which exits on EOF with status 0 and no output — and a
        ``git diff`` that produced no output is indistinguishable from a clean
        repo, so the review came back empty and content.
        """
        configured = self.config.command_mode
        if configured != _DELIVERY_AUTO:
            return configured
        if self.wraps_openssh():
            # Not a guess about somebody's wrapper: ``ssh host 'cmd'`` is OpenSSH's
            # documented interface. Probing it would cost a round trip per config
            # to confirm the manual, and the stdin half of that probe emitted a
            # "did not echo its framing" warning on a perfectly healthy host.
            return _DELIVERY_ARGV
        cached = self.config._resolved_command_mode
        if cached is not None:
            return cached

        probe_timeout = min(timeout, _DELIVERY_PROBE_TIMEOUT_SECONDS)
        # The sentinel is sent split by empty quotes and checked for joined. Only a
        # shell that actually evaluated the line can produce the joined form, so a
        # wrapper that quotes our command back at us in an error message — "Error:
        # Workspace not found: echo __frank""_delivery_ok__" — can no longer look
        # like a success. Combined with the returncode check below, which the first
        # version of this omitted entirely.
        invocation = f'echo {_DELIVERY_SENTINEL_HEAD}""{_DELIVERY_SENTINEL_TAIL}'

        def accepted(probe: ExecResult | None) -> bool:
            return probe is not None and probe.ok and _DELIVERY_SENTINEL in probe.stdout

        def note(candidate: str, probe: ExecResult | None) -> None:
            logger.debug(
                "Remote delivery probe %s failed for %s: %s",
                candidate,
                " ".join(self.config.ssh_command),
                "no result"
                if probe is None
                else f"rc={probe.returncode} out={probe.stdout[:120]!r}",
            )

        # Sequentially, with an early return: a dict literal evaluated both
        # branches every time, so a perfectly healthy OpenSSH host paid an extra
        # round trip per config *and* got a spurious "did not echo its framing"
        # warning — training the operator to ignore the warnings this exists to add.
        argv_probe = self._run_via_argv(invocation, "echo", probe_timeout, None)
        if accepted(argv_probe):
            self.config._resolved_command_mode = _DELIVERY_ARGV
            return _DELIVERY_ARGV
        note(_DELIVERY_ARGV, argv_probe)

        if self._stdin_delivery_runs(invocation, probe_timeout):
            logger.info(
                "Remote %r takes no remote-command argument; driving it over stdin.",
                " ".join(self.config.ssh_command),
            )
            self.config._resolved_command_mode = _DELIVERY_STDIN
            return _DELIVERY_STDIN
        note(_DELIVERY_STDIN, None)

        # Neither worked. Fall back to argv so the failure surfaces as the
        # wrapper's own error message rather than as framing noise, and don't
        # cache it — the host may just be down, and a later cycle should retry.
        logger.warning(
            "Neither delivery shape ran a command through %r. Remote reviews will "
            "produce nothing; check how that command runs a remote command, or set "
            "remote.command_mode explicitly.",
            " ".join(self.config.ssh_command),
        )
        return _DELIVERY_ARGV


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
