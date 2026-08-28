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
import subprocess
import time
import uuid
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
        if not clone_url:
            clone_url = self.config.clone_url_template.format(owner=owner, repo=repo)

        https_fallback = self._https_fallback_url(clone_url)
        ssh_fallback = self._ssh_fallback_url(clone_url)

        remote_dir = self._remote_repo_path(owner, repo, workspace_subdir)
        parent_dir = remote_dir.rsplit("/", 1)[0]

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
            # Through run_script, not a bare subprocess.run: this is the first
            # remote call the review path makes, and going straight to argv here
            # meant a stdin-only wrapper silently ignored the clone script, exited
            # 0 with no output, and had us return a path to a directory that was
            # never created — with every later step then running in it.
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

    def _run_via_stdin(self, remote_invocation: str, label: str, timeout: int) -> ExecResult | None:
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
        # Assembled from two halves at run time so the shell's echo of these lines
        # cannot contain a complete marker — only the executed `echo` can. Same
        # trick as _DELIVERY_SENTINEL, and it means the parser doesn't have to
        # guess which occurrence is real.
        script = "\n".join(
            [
                f"__frank_h='{out_head[:-4]}'",
                f"__frank_e='{err_head[:-4]}'",
                f"__frank_t='{out_tail[:-4]}'",
                f"__frank_n='{nonce[-4:]}'",
                f"echo {begin}",
                "__frank_o=$(mktemp) && __frank_s=$(mktemp)",
                # A subshell, not a { } group. A group runs in the current shell, so
                # a command that ends in `exit` — or a tool that helpfully calls it —
                # terminated the session before any framing was printed, and the whole
                # run came back as "cannot be confirmed to have run at all". In a
                # subshell that exit sets $? and the framing still gets out.
                f'( {remote_invocation} ) > "$__frank_o" 2> "$__frank_s"',
                "__frank_rc=$?",
                # Marker, payload and marker in ONE printf, so they land contiguously.
                # Emitting them as three commands looked tidier and did not work: an
                # interactive shell prints a prompt and echoes the next line between
                # each one, and that echo contains base64-alphabet characters
                # ("base64", "printf", "tr"), so filtering the span down to the
                # base64 alphabet left the command names spliced into the payload.
                'printf "%s%s%s%s%s\\n" "$__frank_h" "$__frank_n" '
                '"$(base64 < "$__frank_o" | tr -d "\\n")" "$__frank_t" "$__frank_n"',
                'printf "%s%s%s%s%s\\n" "$__frank_e" "$__frank_n" '
                '"$(base64 < "$__frank_s" | tr -d "\\n")" "$__frank_t" "$__frank_n"',
                'rm -f "$__frank_o" "$__frank_s"',
                f'echo "{end}:$__frank_rc"',
                "exit",
                "",
            ]
        )
        result = self._spawn(self._ssh_command(), label, timeout, script)
        if result is None:
            return None
        return self._unframe(result, label, begin, end, out_head, err_head, out_tail)

    @staticmethod
    def _unframe(
        result: ExecResult,
        label: str,
        begin: str,
        end: str,
        out_head: str = "",
        err_head: str = "",
        b64_tail: str = "",
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

        stdin_probe = self._run_via_stdin(invocation, "echo", probe_timeout)
        if accepted(stdin_probe):
            logger.info(
                "Remote %r takes no remote-command argument; driving it over stdin.",
                " ".join(self.config.ssh_command),
            )
            self.config._resolved_command_mode = _DELIVERY_STDIN
            return _DELIVERY_STDIN
        note(_DELIVERY_STDIN, stdin_probe)

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
