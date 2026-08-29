"""Tests for the agent-CLI reviewers: config, resolution, and the SSH path.

Written because a real config —

    agent_cli_reviewers:
      - name: "claude"
        enabled: true
        cli_path: claude
        model: "sonnet"
        model_flag: "--model"
        prompt_mode: "flag"
        prompt_arg: "-p"
        remote:
          mode: ssh
          ssh_command: ["sf", "workspace", "ssh"]
          remote_workspace_dir: ~/.frank-remote

— appeared not to fire, and there was no way to tell from a log whether it had.
Every silent exit on that path is now a log line, and these lock that in: the
config surviving load in both the promoted and the explicit shape, the reviewer
resolving, the argv coming out right, and each failure mode saying so.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import (
    AgentCLIReviewerConfig,
    OperatorConfig,
    ProjectConfig,
    RemoteExecutionConfig,
)
from franktheunicorn.core.models import PullRequest
from franktheunicorn.review.agent_cli import run_agent_cli_review
from franktheunicorn.review.tool_executor import ExecResult, RemoteSSHExecutor
from franktheunicorn.worker.runner import (
    _check_agent_cli_reviewers,
    _run_agent_cli_for_pr,
    resolve_agent_cli_reviewers,
)

SSH_YAML = """\
github_username: holdenk
github_token: "x"
agent_cli_reviewers:
  - name: "claude"
    enabled: true
    cli_path: claude
    model: "sonnet"
    model_flag: "--model"
    prompt_mode: "flag"
    prompt_arg: "-p"
    remote:
      mode: ssh
      ssh_command: ["sf", "workspace", "ssh"]
      remote_workspace_dir: ~/.frank-remote
"""

PROMOTED_YAML = """\
github_username: holdenk
github_token: "x"
claude_cli:
  enabled: true
  cli_path: claude
  remote:
    mode: ssh
    ssh_command: ["sf", "workspace", "ssh"]
    remote_workspace_dir: ~/.frank-remote
"""


def _load(tmp_path: Path, text: str) -> OperatorConfig:
    from franktheunicorn.config.loader import load_operator_config

    path = tmp_path / "operator.yaml"
    path.write_text(text)
    return load_operator_config(path)


def _ssh_reviewer(**overrides: Any) -> AgentCLIReviewerConfig:
    fields: dict[str, Any] = {
        "name": "claude",
        "enabled": True,
        "cli_path": "claude",
        "model": "sonnet",
        "model_flag": "--model",
        "prompt_mode": "flag",
        "prompt_arg": "-p",
        "remote": RemoteExecutionConfig(
            mode="ssh",
            ssh_command=["sf", "workspace", "ssh"],
            remote_workspace_dir="~/.frank-remote",
        ),
    }
    fields.update(overrides)
    return AgentCLIReviewerConfig(**fields)


class TestConfigReachesTheReviewer:
    """Both spellings have to survive the loader with the remote block intact."""

    def test_the_explicit_list_form_resolves(self, tmp_path: Path) -> None:
        oc = _load(tmp_path, SSH_YAML)

        names = [rc.name for rc in resolve_agent_cli_reviewers(oc)]

        assert "claude" in names

    def test_the_explicit_list_form_keeps_its_remote_block(self, tmp_path: Path) -> None:
        oc = _load(tmp_path, SSH_YAML)

        claude = next(rc for rc in oc.agent_cli_reviewers if rc.name == "claude")

        assert claude.remote.mode == "ssh"
        assert claude.remote.ssh_command == ["sf", "workspace", "ssh"]
        assert claude.remote.remote_workspace_dir == "~/.frank-remote"
        assert claude.model == "sonnet"

    def test_the_promoted_root_level_form_resolves_too(self, tmp_path: Path) -> None:
        """operator.yaml documents the top-level ``claude_cli:`` block as still
        accepted and promoted into the registry."""
        oc = _load(tmp_path, PROMOTED_YAML)

        claude = next(rc for rc in oc.agent_cli_reviewers if rc.name == "claude")

        assert claude.enabled is True
        assert claude.remote.mode == "ssh"
        assert claude.remote.ssh_command == ["sf", "workspace", "ssh"]
        assert "claude" in [rc.name for rc in resolve_agent_cli_reviewers(oc)]

    def test_promotion_does_not_leave_a_duplicate_claude(self, tmp_path: Path) -> None:
        """Two entries would run the reviewer twice per PR."""
        oc = _load(tmp_path, PROMOTED_YAML)

        assert [rc.name for rc in oc.agent_cli_reviewers].count("claude") == 1

    def test_an_ssh_reviewer_is_enabled_without_a_local_binary(self) -> None:
        """The binary lives on the remote, so PATH here says nothing."""
        oc = OperatorConfig(
            agent_cli_reviewers=[_ssh_reviewer(cli_path="definitely-not-installed")]
        )

        # Membership, not equality: OperatorConfig seeds the built-in codex/pi
        # entries, and whether those resolve depends on this box's PATH.
        assert "claude" in [rc.name for rc in resolve_agent_cli_reviewers(oc)]


class TestInvocation:
    """The argv the YAML actually produces."""

    def test_the_model_flag_and_prompt_flag_are_both_used(self) -> None:
        rc = _ssh_reviewer()

        argv = list(rc.cli_argv) + rc.build_invocation("REVIEW THIS")

        assert argv[0] == "claude"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert "-p" in argv
        assert argv[-1] == "REVIEW THIS"

    def test_the_command_is_logged_before_it_runs(self, caplog: Any) -> None:
        """Otherwise there is no way to tell a misassembled argv from a
        reviewer that never ran — both produce zero findings."""
        executor = MagicMock()
        executor.run.side_effect = [
            ExecResult(returncode=0, stdout="diff --git a/x b/x\n+y\n", stderr=""),
            ExecResult(returncode=0, stdout="Review completed", stderr=""),
        ]

        with caplog.at_level(logging.INFO):
            run_agent_cli_review("/w", "base", _ssh_reviewer(), executor=executor)

        assert any("running claude" in r.getMessage() for r in caplog.records)


class TestFailuresAreLogged:
    """Each of these used to return [] with nothing at INFO or above."""

    def test_a_dead_executor_is_an_error_not_a_silence(self, caplog: Any) -> None:
        """`run` returning None is "the ssh_command did not come back"."""
        executor = MagicMock()
        executor.run.side_effect = [
            ExecResult(returncode=0, stdout="diff --git a/x b/x\n+y\n", stderr=""),
            None,
        ]

        with caplog.at_level(logging.INFO):
            findings = run_agent_cli_review("/w", "base", _ssh_reviewer(), executor=executor)

        assert findings == []
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "a reviewer that could not be reached must say so"
        assert "ssh_command" in errors[0].getMessage()

    def test_a_failed_git_diff_warns(self, caplog: Any) -> None:
        executor = MagicMock()
        executor.run.return_value = ExecResult(returncode=128, stdout="", stderr="bad revision")

        with caplog.at_level(logging.INFO):
            findings = run_agent_cli_review("/w", "base", _ssh_reviewer(), executor=executor)

        assert findings == []
        assert any(r.levelno >= logging.WARNING for r in caplog.records)
        assert "bad revision" in caplog.text

    def test_an_empty_diff_says_so_at_info(self, caplog: Any) -> None:
        """The checkout not having the PR head fetched looks exactly like a
        reviewer that never fired."""
        executor = MagicMock()
        executor.run.return_value = ExecResult(returncode=0, stdout="   \n", stderr="")

        with caplog.at_level(logging.INFO):
            findings = run_agent_cli_review("/w", "base", _ssh_reviewer(), executor=executor)

        assert findings == []
        assert "no diff between" in caplog.text

    def test_a_clean_review_is_distinguishable_from_no_review(self, caplog: Any) -> None:
        executor = MagicMock()
        executor.run.side_effect = [
            ExecResult(returncode=0, stdout="diff --git a/x b/x\n+y\n", stderr=""),
            ExecResult(returncode=0, stdout="Review completed", stderr=""),
        ]

        with caplog.at_level(logging.INFO):
            findings = run_agent_cli_review("/w", "base", _ssh_reviewer(), executor=executor)

        assert findings == []
        assert "returned no findings" in caplog.text

    def test_a_nonzero_exit_reports_stderr(self, caplog: Any) -> None:
        executor = MagicMock()
        executor.run.side_effect = [
            ExecResult(returncode=0, stdout="diff --git a/x b/x\n+y\n", stderr=""),
            ExecResult(returncode=127, stdout="", stderr="claude: command not found"),
        ]

        with caplog.at_level(logging.INFO):
            run_agent_cli_review("/w", "base", _ssh_reviewer(), executor=executor)

        assert "command not found" in caplog.text


@pytest.mark.django_db
class TestNoCheckoutIsLoud:
    """`_run_review_tool_for_pr` used to `return` with no log when no checkout
    could be prepared, which is the commonest way an ssh reviewer goes missing."""

    def test_a_failed_remote_prepare_warns(self, db_pr: PullRequest, caplog: Any) -> None:
        with (
            patch.object(RemoteSSHExecutor, "prepare_repo", return_value=None),
            caplog.at_level(logging.INFO),
        ):
            _run_agent_cli_for_pr(
                db_pr,
                _ssh_reviewer(),
                repo_path=None,
                clone_url="https://example.com/a/b.git",
                project_config=ProjectConfig(owner="apache", repo="spark"),
            )

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a reviewer that got no checkout must say so"
        assert "did not run" in caplog.text or "Could not prepare" in caplog.text

    def test_zero_findings_is_still_reported(self, db_pr: PullRequest, caplog: Any) -> None:
        """ "It ran and the PR is clean" and "it never ran" were the same
        empty log."""
        db_pr.head_sha = "a" * 40
        db_pr.save()

        with (
            patch.object(RemoteSSHExecutor, "prepare_repo", return_value="/remote/spark"),
            patch(
                "franktheunicorn.worker.runner._resolve_remote_base_ref",
                return_value="origin/master",
            ),
            patch.object(
                RemoteSSHExecutor,
                "run",
                return_value=ExecResult(returncode=0, stdout="", stderr=""),
            ),
            patch(
                "franktheunicorn.review.agent_cli.run_agent_cli_review",
                return_value=[],
            ),
            caplog.at_level(logging.INFO),
        ):
            _run_agent_cli_for_pr(
                db_pr,
                _ssh_reviewer(),
                repo_path=None,
                clone_url="https://example.com/a/b.git",
                project_config=ProjectConfig(owner="apache", repo="spark"),
            )

        assert "produced 0 finding(s)" in caplog.text


class TestStartupProbe:
    """An ssh reviewer is enabled optimistically, so startup is the only place
    the remote binary gets checked at all."""

    def test_a_wrapper_that_ignores_the_command_is_caught(self, caplog: Any) -> None:
        """The failure nothing else can see: exit 0, empty stdout, command never
        run. Downstream that is indistinguishable from a repo with no diff, so
        the review comes back clean and silent."""
        oc = OperatorConfig(agent_cli_reviewers=[_ssh_reviewer()])

        with (
            patch.object(
                RemoteSSHExecutor,
                "run",
                return_value=ExecResult(returncode=0, stdout="", stderr=""),
            ),
            caplog.at_level(logging.INFO),
        ):
            _check_agent_cli_reviewers(oc)

        assert "did not run our command" in caplog.text
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_a_missing_remote_binary_warns_at_startup(self, caplog: Any) -> None:
        oc = OperatorConfig(agent_cli_reviewers=[_ssh_reviewer()])

        with (
            patch.object(
                RemoteSSHExecutor,
                "run",
                side_effect=[
                    # The sentinel round-trips, so the command path is fine...
                    ExecResult(returncode=0, stdout="frank-remote-ok\n", stderr=""),
                    # ...the workspace is there...
                    ExecResult(returncode=0, stdout="/home/u/.frank-remote\n", stderr=""),
                    # ...but the binary is not.
                    ExecResult(returncode=1, stdout="", stderr="not found"),
                ],
            ),
            caplog.at_level(logging.INFO),
        ):
            _check_agent_cli_reviewers(oc)

        assert "not on the remote PATH" in caplog.text
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_an_unreachable_remote_warns_at_startup(self, caplog: Any) -> None:
        oc = OperatorConfig(agent_cli_reviewers=[_ssh_reviewer()])

        with (
            patch.object(RemoteSSHExecutor, "run", return_value=None),
            caplog.at_level(logging.INFO),
        ):
            _check_agent_cli_reviewers(oc)

        assert "sf workspace ssh" in caplog.text
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_a_present_remote_binary_is_reported_ok(self, caplog: Any) -> None:
        oc = OperatorConfig(agent_cli_reviewers=[_ssh_reviewer()])

        with (
            patch.object(
                RemoteSSHExecutor,
                "run",
                side_effect=[
                    ExecResult(returncode=0, stdout="frank-remote-ok\n", stderr=""),
                    ExecResult(returncode=0, stdout="/home/u/.frank-remote\n", stderr=""),
                    ExecResult(returncode=0, stdout="/usr/local/bin/claude\n", stderr=""),
                ],
            ),
            caplog.at_level(logging.INFO),
        ):
            _check_agent_cli_reviewers(oc)

        assert "/usr/local/bin/claude" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_every_configured_reviewer_gets_a_verdict(self, caplog: Any) -> None:
        """Silence about a reviewer is the thing being fixed, so no configured
        entry may go unmentioned."""
        oc = OperatorConfig(
            agent_cli_reviewers=[
                _ssh_reviewer(),
                AgentCLIReviewerConfig(name="codex", enabled=False),
                AgentCLIReviewerConfig(name="pi", enabled="auto", cli_path="pi-not-installed"),
            ]
        )

        with (
            patch.object(
                RemoteSSHExecutor,
                "run",
                side_effect=[
                    ExecResult(returncode=0, stdout="frank-remote-ok\n", stderr=""),
                    ExecResult(returncode=0, stdout="/home/u/.frank-remote\n", stderr=""),
                    ExecResult(returncode=0, stdout="/bin/claude\n", stderr=""),
                ],
            ),
            caplog.at_level(logging.INFO),
        ):
            _check_agent_cli_reviewers(oc)

        for name in ("claude", "codex", "pi"):
            assert f"'{name}'" in caplog.text, f"{name} got no verdict line"

    def test_a_missing_remote_workspace_is_named_at_startup(self, caplog: Any) -> None:
        """The review's failure message used to tell the operator to check this by
        hand; it is one round trip, so it is checked once here instead."""
        oc = OperatorConfig(agent_cli_reviewers=[_ssh_reviewer()])

        with (
            patch.object(
                RemoteSSHExecutor,
                "run",
                side_effect=[
                    ExecResult(returncode=0, stdout="frank-remote-ok\n", stderr=""),
                    ExecResult(returncode=1, stdout="", stderr="No such file or directory"),
                    ExecResult(returncode=0, stdout="/bin/claude\n", stderr=""),
                ],
            ),
            caplog.at_level(logging.INFO),
        ):
            _check_agent_cli_reviewers(oc)

        assert "does not exist on the remote" in caplog.text
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_the_probe_never_disables_a_reviewer(self, caplog: Any) -> None:
        """A probe that fails for its own reasons — a slow bastion, a wrapper
        wanting a TTY — must not switch off a working reviewer."""
        oc = OperatorConfig(agent_cli_reviewers=[_ssh_reviewer()])

        with (
            patch.object(RemoteSSHExecutor, "run", side_effect=RuntimeError("boom")),
            caplog.at_level(logging.INFO),
        ):
            _check_agent_cli_reviewers(oc)

        assert "claude" in [rc.name for rc in resolve_agent_cli_reviewers(oc)]
        assert "leaving it enabled" in caplog.text


class TestCursorReviewer:
    """``cursor-agent``, seeded read-only.

    Its interface differs from claude's in a way that happens to converge:
    ``-p``/``--print`` is a boolean and the prompt is positional, where claude's
    ``-p`` carries the prompt. Both produce ``[-p, <prompt>]``.
    """

    @staticmethod
    def _cursor() -> AgentCLIReviewerConfig:
        return next(rc for rc in OperatorConfig().agent_cli_reviewers if rc.name == "cursor-agent")

    def test_the_binary_is_cursor_agent_not_cursor(self) -> None:
        """``cursor`` is the editor; ``cursor-agent`` is the headless CLI. The seed
        is named after the binary because cli_path defaults to name, so a friendlier
        name would have sent it looking for the wrong executable."""
        assert self._cursor().cli_argv == ["cursor-agent"]
        assert self._cursor().name == "cursor-agent"

    def test_it_runs_read_only(self) -> None:
        """``-p`` alone is documented as having "access to all tools, including
        write and shell", and this is a reviewer pointed at someone else's PR."""
        argv = list(self._cursor().cli_argv) + self._cursor().build_invocation("REVIEW")

        assert "--mode" in argv
        assert argv[argv.index("--mode") + 1] == "ask"

    def test_the_prompt_is_the_trailing_argument(self) -> None:
        argv = list(self._cursor().cli_argv) + self._cursor().build_invocation("REVIEW")

        assert argv == ["cursor-agent", "--trust", "--mode", "ask", "-p", "REVIEW"]

    def test_it_passes_trust_because_otherwise_it_refuses_to_run(self) -> None:
        """Not a nicety. Every checkout frank drives an agent in is one frank
        created, so the first run in each is in a workspace cursor-agent has never
        been told to trust — and it exits 1 with empty stdout and "⚠ Workspace Trust
        Required", advising you to run it interactively, which a worker cannot.
        Verified against the real binary, along with ``--trust`` fixing it.

        ``--trust`` specifically, and not ``--yolo``: its own --help gives ``--yolo``
        as an alias for ``--force`` ("Force allow commands unless explicitly
        denied"), which is a far larger grant than "this is the directory I meant"
        and would undo the point of ``--mode ask``.
        """
        cursor = self._cursor()

        assert cursor.trust_args == ["--trust"]
        assert "--yolo" not in cursor.trust_args
        assert "--force" not in cursor.trust_args

    def test_claude_needs_no_trust_flag(self) -> None:
        """``claude --help``: "the workspace trust dialog is skipped when Claude is
        run in non-interactive mode (via -p, or when stdout is not a TTY)".
        Confirmed by running it in a directory absent from ~/.claude.json's project
        map — exit 0, and no entry added. So an empty list here is the correct
        answer, not an oversight."""
        claude = next(rc for rc in OperatorConfig().agent_cli_reviewers if rc.name == "claude")

        assert claude.trust_args == []

    def test_trust_survives_an_operator_overriding_the_mode(self) -> None:
        """The reason trust lives in its own field. Entries merge by *name*, not by
        field, so an operator setting ``extra_args`` replaces the seed's outright —
        which is exactly what someone changing ``--mode`` does, and folding trust in
        there would have silently broken their reviewer."""
        rc = AgentCLIReviewerConfig(
            name="cursor-agent", trust_args=["--trust"], extra_args=["--mode", "agent"]
        )

        argv = list(rc.cli_argv) + rc.build_invocation("REVIEW")

        assert argv == ["cursor-agent", "--trust", "--mode", "agent", "-p", "REVIEW"]

    def test_trust_args_land_after_the_subcommand_in_subcommand_mode(self) -> None:
        """A flag ahead of ``exec`` is a flag to the wrong parser."""
        rc = AgentCLIReviewerConfig(
            name="codex-ish",
            cli_path="codex",
            prompt_mode="subcommand",
            prompt_arg="exec",
            trust_args=["--trust-me"],
        )

        assert rc.build_invocation("REVIEW") == ["exec", "--trust-me", "REVIEW"]

    def test_it_is_auto_detected_not_forced_on(self) -> None:
        """Same contract as the other seeds: runs only if the binary is present."""
        assert self._cursor().enabled == "auto"

    def test_a_model_override_uses_cursors_own_flag(self) -> None:
        """``cursor-agent --model <m>`` matches the default model_flag."""
        rc = AgentCLIReviewerConfig(
            name="cursor-agent", model="gpt-5", extra_args=["--mode", "ask"]
        )

        argv = list(rc.cli_argv) + rc.build_invocation("REVIEW")

        assert argv == ["cursor-agent", "--model", "gpt-5", "--mode", "ask", "-p", "REVIEW"]

    def test_an_operator_can_still_override_the_mode(self) -> None:
        """Seeds merge field-by-field, so overriding extra_args replaces only that."""
        import tempfile

        from franktheunicorn.config.loader import load_operator_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operator.yaml"
            path.write_text(
                "github_token: x\n"
                "agent_cli_reviewers:\n"
                '  - name: "cursor-agent"\n'
                "    extra_args: []\n"
            )
            oc = load_operator_config(path)

        cursor = next(rc for rc in oc.agent_cli_reviewers if rc.name == "cursor-agent")
        assert cursor.extra_args == []
        assert cursor.cli_argv == ["cursor-agent"], "the seeded cli_path survives"
