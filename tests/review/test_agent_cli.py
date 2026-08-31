"""Tests for the generalized agent-CLI review integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import (
    AgentCLIReviewerConfig,
    ClaudeCLIConfig,
    OperatorConfig,
    RemoteExecutionConfig,
)
from franktheunicorn.core.models import Project, PullRequest
from franktheunicorn.review.agent_cli import (
    AgentCLIFinding,
    build_review_prompt,
    create_drafts_from_agent_cli,
    run_agent_cli_review,
)
from franktheunicorn.review.tool_executor import ExecResult
from franktheunicorn.worker.runner import resolve_agent_cli_reviewers
from tests.factories import AntiPatternFactory

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _executor_returning(*results: ExecResult | None) -> MagicMock:
    """Build a mock ToolExecutor whose ``run`` yields the given results."""
    mock = MagicMock()
    mock.run.side_effect = list(results)
    return mock


# ---------------------------------------------------------------------------
# build_invocation — flag vs subcommand + model flag
# ---------------------------------------------------------------------------


class TestBuildInvocation:
    # These two use "pi", not "claude": the claude entry carries a default model
    # (default_claude_model), and the subject here is argv assembly with none.
    def test_flag_mode_prompt_follows_flag(self) -> None:
        cfg = AgentCLIReviewerConfig(name="pi", prompt_mode="flag", prompt_arg="-p")
        assert cfg.build_invocation("HELLO") == ["-p", "HELLO"]

    def test_flag_mode_with_model(self) -> None:
        cfg = AgentCLIReviewerConfig(
            name="pi", prompt_mode="flag", prompt_arg="-p", model="google/gemini"
        )
        assert cfg.build_invocation("HELLO") == ["--model", "google/gemini", "-p", "HELLO"]

    def test_subcommand_mode_prompt_is_trailing_positional(self) -> None:
        cfg = AgentCLIReviewerConfig(name="codex", prompt_mode="subcommand", prompt_arg="exec")
        assert cfg.build_invocation("HELLO") == ["exec", "HELLO"]

    def test_subcommand_mode_with_model_and_extra_args(self) -> None:
        cfg = AgentCLIReviewerConfig(
            name="codex",
            prompt_mode="subcommand",
            prompt_arg="exec",
            model="gpt-5",
            extra_args=["--full-auto"],
        )
        assert cfg.build_invocation("P") == ["exec", "--model", "gpt-5", "--full-auto", "P"]

    def test_custom_model_flag(self) -> None:
        cfg = AgentCLIReviewerConfig(
            name="codex", prompt_mode="subcommand", prompt_arg="exec", model="o3", model_flag="-m"
        )
        assert cfg.build_invocation("P") == ["exec", "-m", "o3", "P"]

    def test_cli_argv_defaults_to_name(self) -> None:
        cfg = AgentCLIReviewerConfig(name="codex", prompt_mode="subcommand", prompt_arg="exec")
        assert cfg.cli_argv == ["codex"]

    def test_full_argv_flag(self) -> None:
        cfg = AgentCLIReviewerConfig(name="pi", cli_path="pi", prompt_arg="-p")
        assert cfg.cli_argv + cfg.build_invocation("PROMPT") == ["pi", "-p", "PROMPT"]

    def test_full_argv_subcommand(self) -> None:
        cfg = AgentCLIReviewerConfig(
            name="codex", cli_path="codex", prompt_mode="subcommand", prompt_arg="exec"
        )
        assert cfg.cli_argv + cfg.build_invocation("PROMPT") == ["codex", "exec", "PROMPT"]

    def test_full_argv_ordering_subcommand_prompt_trailing(self) -> None:
        # Subcommand ALWAYS first, model + extra args in the middle, prompt
        # ALWAYS the trailing positional. Guards against build_invocation drift.
        cfg = AgentCLIReviewerConfig(
            name="codex",
            cli_path="codex",
            prompt_mode="subcommand",
            prompt_arg="exec",
            model="o3",
            extra_args=["--full-auto", "--json"],
        )
        assert cfg.cli_argv + cfg.build_invocation("PROMPT") == [
            "codex",
            "exec",
            "--model",
            "o3",
            "--full-auto",
            "--json",
            "PROMPT",
        ]

    def test_full_argv_ordering_flag_prompt_trailing(self) -> None:
        cfg = AgentCLIReviewerConfig(
            name="claude",
            cli_path="claude",
            prompt_mode="flag",
            prompt_arg="-p",
            model="opus",
            extra_args=["--permission-mode", "plan"],
        )
        assert cfg.cli_argv + cfg.build_invocation("PROMPT") == [
            "claude",
            "--model",
            "opus",
            "--permission-mode",
            "plan",
            "-p",
            "PROMPT",
        ]


# ---------------------------------------------------------------------------
# CLI runner tests (via mocked executor)
# ---------------------------------------------------------------------------


class TestRunAgentCLIReview:
    def _config(self, **overrides: Any) -> AgentCLIReviewerConfig:
        base: dict[str, Any] = {"name": "codex", "prompt_mode": "subcommand", "prompt_arg": "exec"}
        base.update(overrides)
        return AgentCLIReviewerConfig(**base)

    def test_parses_findings_from_cli_output(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n+pass\n", stderr="")
        cli_result = ExecResult(
            returncode=0,
            stdout=(FIXTURES_DIR / "claude_cli_output.txt").read_text(),
            stderr="",
        )
        executor = _executor_returning(diff_result, cli_result)
        findings = run_agent_cli_review(
            cwd="/tmp/repo", base_commit="origin/main", config=self._config(), executor=executor
        )
        assert len(findings) == 3
        assert findings[0].file_path == "src/auth/session.py"
        assert findings[0].severity == "high"

    def test_subcommand_prompt_is_positional(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n+pass\n", stderr="")
        cli_result = ExecResult(returncode=0, stdout="Review completed", stderr="")
        executor = _executor_returning(diff_result, cli_result)
        run_agent_cli_review(
            cwd="/tmp/repo", base_commit="abc", config=self._config(), executor=executor
        )
        cmd = executor.run.call_args_list[1].args[0]
        # codex exec <prompt> — subcommand first, prompt is the last argument.
        assert cmd[:2] == ["codex", "exec"]
        assert "You are a senior code reviewer" in cmd[-1]

    def test_empty_diff_skips_cli(self) -> None:
        executor = _executor_returning(ExecResult(returncode=0, stdout="", stderr=""))
        findings = run_agent_cli_review(
            cwd="/tmp/repo", base_commit="origin/main", config=self._config(), executor=executor
        )
        assert findings == []
        assert executor.run.call_count == 1

    def test_diff_failure_returns_empty(self) -> None:
        executor = _executor_returning(ExecResult(returncode=128, stdout="", stderr="not a repo"))
        assert (
            run_agent_cli_review(
                cwd="/tmp/repo", base_commit="origin/main", config=self._config(), executor=executor
            )
            == []
        )

    def test_missing_binary_returns_empty(self) -> None:
        # LocalExecutor.run returns None when the binary is absent (FileNotFoundError).
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n", stderr="")
        executor = _executor_returning(diff_result, None)
        assert (
            run_agent_cli_review(
                cwd="/tmp/repo", base_commit="origin/main", config=self._config(), executor=executor
            )
            == []
        )

    def test_cli_nonzero_exit_returns_empty(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n", stderr="")
        cli_result = ExecResult(returncode=1, stdout="", stderr="rate limit")
        executor = _executor_returning(diff_result, cli_result)
        assert (
            run_agent_cli_review(
                cwd="/tmp/repo", base_commit="origin/main", config=self._config(), executor=executor
            )
            == []
        )

    def test_unparseable_output_returns_empty(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n", stderr="")
        cli_result = ExecResult(returncode=0, stdout="Review completed", stderr="")
        executor = _executor_returning(diff_result, cli_result)
        assert (
            run_agent_cli_review(
                cwd="/tmp/repo", base_commit="origin/main", config=self._config(), executor=executor
            )
            == []
        )

    def test_diff_truncated_when_oversize(self) -> None:
        big_diff = "+x\n" * 50_000
        diff_result = ExecResult(returncode=0, stdout=big_diff, stderr="")
        cli_result = ExecResult(returncode=0, stdout="Review completed", stderr="")
        executor = _executor_returning(diff_result, cli_result)
        run_agent_cli_review(
            cwd="/tmp/repo",
            base_commit="abc",
            config=self._config(max_diff_chars=10_000),
            executor=executor,
        )
        prompt = executor.run.call_args_list[1].args[0][-1]
        assert "[...diff truncated...]" in prompt

    def test_model_flag_passed_through(self) -> None:
        diff_result = ExecResult(returncode=0, stdout="diff --git a/x b/x\n", stderr="")
        cli_result = ExecResult(returncode=0, stdout="Review completed", stderr="")
        executor = _executor_returning(diff_result, cli_result)
        run_agent_cli_review(
            cwd="/tmp/repo",
            base_commit="abc",
            config=self._config(model="gpt-5"),
            executor=executor,
        )
        cmd = executor.run.call_args_list[1].args[0]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-5"


# ---------------------------------------------------------------------------
# review_focus="security" — the security-only prompt + trust-boundary injection
# ---------------------------------------------------------------------------


class TestSecurityFocus:
    def _security_config(self, **overrides: Any) -> AgentCLIReviewerConfig:
        base: dict[str, Any] = {
            "name": "cursor-agent-security",
            "cli_path": "cursor-agent",
            "review_focus": "security",
        }
        base.update(overrides)
        return AgentCLIReviewerConfig(**base)

    def _run_and_capture_prompt(self, config: AgentCLIReviewerConfig, **kwargs: Any) -> str:
        executor = _executor_returning(
            ExecResult(returncode=0, stdout="diff --git a/x b/x\n+pass\n", stderr=""),
            ExecResult(returncode=0, stdout="Review completed", stderr=""),
        )
        run_agent_cli_review(
            cwd="/tmp/repo", base_commit="abc", config=config, executor=executor, **kwargs
        )
        return executor.run.call_args_list[1].args[0][-1]

    def test_default_focus_is_general(self) -> None:
        cfg = AgentCLIReviewerConfig(name="pi")
        assert cfg.review_focus == "general"
        assert build_review_prompt(cfg, "DIFF").startswith("You are a senior code reviewer")

    def test_an_unknown_focus_is_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentCLIReviewerConfig(name="pi", review_focus="sec")  # type: ignore[arg-type]

    def test_security_focus_uses_the_security_prompt(self) -> None:
        prompt = self._run_and_capture_prompt(self._security_config())
        assert "senior security reviewer" in prompt
        assert "You are a senior code reviewer" not in prompt

    def test_the_security_model_is_prepended_to_the_diff(self) -> None:
        prompt = self._run_and_capture_prompt(
            self._security_config(),
            security_model="Submitted jobs run arbitrary code by design.",
        )
        assert "Submitted jobs run arbitrary code by design." in prompt
        # Ahead of the diff, so the model reads the stance before the code.
        assert prompt.index("Submitted jobs") < prompt.index("diff --git")

    def test_a_missing_security_model_says_so_in_the_prompt(self) -> None:
        """Otherwise the agent invents a stance for the project."""
        prompt = self._run_and_capture_prompt(self._security_config(), security_model="")
        assert "has not documented a security model" in prompt

    def test_a_general_reviewer_ignores_the_security_model(self) -> None:
        prompt = build_review_prompt(
            AgentCLIReviewerConfig(name="pi"), "DIFF", security_model="LEAK ME NOT"
        )
        assert "LEAK ME NOT" not in prompt

    def test_security_findings_parse_with_the_shared_parser(self) -> None:
        """The security prompt keeps the block-format output contract, and the
        "security:" title prefix the prompt asks for lands in the finding body —
        posted comments stand alone, without the source label next to them."""
        output = (
            "python/pyspark/sql/io.py:42 - [High] security: crafted footer triggers class load\n"
            "\n"
            "The Parquet footer path passes attacker-controlled bytes to reflection.\n"
            "\n"
            "**Suggestion:** Restrict the class allowlist.\n"
            "\n"
            "=============\n"
            "Review completed\n"
        )
        executor = _executor_returning(
            ExecResult(returncode=0, stdout="diff --git a/x b/x\n+pass\n", stderr=""),
            ExecResult(returncode=0, stdout=output, stderr=""),
        )
        findings = run_agent_cli_review(
            cwd="/tmp/repo",
            base_commit="abc",
            config=self._security_config(),
            executor=executor,
        )
        assert len(findings) == 1
        assert findings[0].severity == "high"
        assert findings[0].body.startswith("security: crafted footer triggers class load")


# ---------------------------------------------------------------------------
# Draft creation + cross-agent dedup
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateDraftsFromAgentCLI:
    def _finding(self, **overrides: Any) -> AgentCLIFinding:
        base: dict[str, Any] = {
            "file_path": "src/x.py",
            "line_number": 10,
            "severity": "high",
            "title": "Null deref",
            "body": "The value may be None when the cache misses and is dereferenced.",
            "suggestion": "Guard against None.",
        }
        base.update(overrides)
        return AgentCLIFinding(**base)

    def test_creates_draft_attributed_to_source(self, db_pr: PullRequest) -> None:
        drafts = create_drafts_from_agent_cli(db_pr, [self._finding()], source="codex")
        assert len(drafts) == 1
        assert drafts[0].sources == ["codex"]
        assert drafts[0].backend_used == "codex"
        assert drafts[0].confidence == pytest.approx(0.8)

    def test_anti_pattern_suppression(self, db_pr: PullRequest, db_project: Project) -> None:
        AntiPatternFactory(pattern_text="rename to", project=db_project)
        findings = [
            self._finding(file_path="a.py", body="Please rename to something better."),
            self._finding(file_path="b.py", body="Genuine race condition here."),
        ]
        drafts = create_drafts_from_agent_cli(db_pr, findings, project=db_project, source="claude")
        assert len(drafts) == 1
        assert drafts[0].file_path == "b.py"

    def test_dedup_merges_attribution_across_agents(self, db_pr: PullRequest) -> None:
        # Agent A flags the spot.
        a = create_drafts_from_agent_cli(db_pr, [self._finding()], source="claude")
        assert len(a) == 1
        # Agent B flags the same file+line — no new draft, attribution merges.
        b = create_drafts_from_agent_cli(db_pr, [self._finding()], source="codex")
        assert b == []
        assert db_pr.review_drafts.count() == 1
        merged = db_pr.review_drafts.get()
        assert merged.sources == ["claude", "codex"]

    def test_dedup_disabled_creates_second_draft(self, db_pr: PullRequest) -> None:
        create_drafts_from_agent_cli(db_pr, [self._finding()], source="claude")
        b = create_drafts_from_agent_cli(
            db_pr, [self._finding()], source="codex", deduplicate=False
        )
        assert len(b) == 1
        assert db_pr.review_drafts.count() == 2

    def test_distant_findings_not_merged(self, db_pr: PullRequest) -> None:
        create_drafts_from_agent_cli(db_pr, [self._finding(line_number=10)], source="claude")
        # Far away on the same file → distinct finding, own draft.
        b = create_drafts_from_agent_cli(
            db_pr,
            [self._finding(line_number=400, body="Totally different unrelated issue.")],
            source="codex",
        )
        assert len(b) == 1
        assert db_pr.review_drafts.count() == 2


# ---------------------------------------------------------------------------
# Auto-enablement resolution ("use if installed")
# ---------------------------------------------------------------------------


class TestResolveAgentCLIReviewers:
    def _only_codex(self, **codex_overrides: Any) -> OperatorConfig:
        """Config where every other seeded reviewer is force-disabled.

        Derived from the seed list rather than naming claude/pi literally:
        OperatorConfig appends any built-in the operator didn't list, so a newly
        seeded reviewer whose binary happens to be installed on the test machine
        would otherwise leak into every assertion here.
        """
        from franktheunicorn.config.models import _default_agent_cli_reviewers

        others = [
            AgentCLIReviewerConfig(name=seed.name, enabled=False)
            for seed in _default_agent_cli_reviewers()
            if seed.name != "codex"
        ]
        return OperatorConfig(
            agent_cli_reviewers=[
                *others,
                AgentCLIReviewerConfig(name="codex", cli_path="codex", **codex_overrides),
            ]
        )

    def test_none_operator_config_returns_empty(self) -> None:
        assert resolve_agent_cli_reviewers(None) == []

    def test_auto_enabled_when_binary_present(self) -> None:
        cfg = self._only_codex()
        with patch("franktheunicorn.worker.runner.shutil.which", return_value="/usr/bin/codex"):
            resolved = resolve_agent_cli_reviewers(cfg)
        assert [r.name for r in resolved] == ["codex"]

    def test_auto_skipped_when_binary_absent(self) -> None:
        cfg = self._only_codex()
        with patch("franktheunicorn.worker.runner.shutil.which", return_value=None):
            resolved = resolve_agent_cli_reviewers(cfg)
        assert resolved == []

    def test_explicit_true_overrides_missing_binary(self) -> None:
        cfg = self._only_codex(enabled=True)
        with patch("franktheunicorn.worker.runner.shutil.which", return_value=None):
            resolved = resolve_agent_cli_reviewers(cfg)
        assert [r.name for r in resolved] == ["codex"]

    def test_explicit_false_overrides_present_binary(self) -> None:
        cfg = self._only_codex(enabled=False)
        with patch("franktheunicorn.worker.runner.shutil.which", return_value="/usr/bin/codex"):
            resolved = resolve_agent_cli_reviewers(cfg)
        assert resolved == []

    def test_auto_ssh_enabled_optimistically_without_probe(self) -> None:
        cfg = self._only_codex(remote=RemoteExecutionConfig(mode="ssh", host="h"))
        # which() must not be consulted for SSH-mode auto reviewers (claude/pi
        # are force-disabled, so they never probe either).
        with patch(
            "franktheunicorn.worker.runner.shutil.which", side_effect=AssertionError("probed")
        ):
            resolved = resolve_agent_cli_reviewers(cfg)
        assert [r.name for r in resolved] == ["codex"]


# ---------------------------------------------------------------------------
# Back-compat: legacy claude_cli promotion into the registry
# ---------------------------------------------------------------------------


class TestLegacyClaudeCLIPromotion:
    def test_seeds_the_default_reviewers(self) -> None:
        oc = OperatorConfig()
        assert [r.name for r in oc.agent_cli_reviewers] == [
            "claude",
            "codex",
            "pi",
            "cursor-agent",
        ]
        assert all(r.enabled == "auto" for r in oc.agent_cli_reviewers)

    def test_every_seed_is_named_after_its_binary(self) -> None:
        """``cli_path`` defaults to ``name``, so a seed named anything else sends
        the reviewer looking for an executable that does not exist — which is how
        a ``- name: cursor`` entry would have quietly never run."""
        for rc in OperatorConfig().agent_cli_reviewers:
            assert rc.cli_argv[0] == rc.name

    def test_claude_defaults_to_sonnet_and_the_others_to_the_cli_default(self) -> None:
        """Argv too, not just the field — an empty model emits no flag at all."""
        oc = OperatorConfig()
        by_name = {r.name: r for r in oc.agent_cli_reviewers}
        assert by_name["claude"].model == "sonnet"
        assert by_name["claude"].build_invocation("p") == ["--model", "sonnet", "-p", "p"]
        # Left alone deliberately — this default is about the claude CLI.
        assert by_name["codex"].model == ""
        assert by_name["pi"].model == ""

    def test_a_legacy_block_without_a_model_also_gets_sonnet(self) -> None:
        """Both doors onto the same CLI agree."""
        oc = OperatorConfig(claude_cli=ClaudeCLIConfig(enabled=True))
        claude = next(r for r in oc.agent_cli_reviewers if r.name == "claude")
        assert claude.model == "sonnet"

    def test_an_explicit_model_still_wins(self) -> None:
        oc = OperatorConfig(
            agent_cli_reviewers=[AgentCLIReviewerConfig(name="claude", model="opus")]
        )
        claude = next(r for r in oc.agent_cli_reviewers if r.name == "claude")
        assert claude.model == "opus"

    def test_overriding_one_field_keeps_the_model_default(self) -> None:
        """operator.yaml advertises this shape; it used to land on the CLI default."""
        oc = OperatorConfig(
            agent_cli_reviewers=[AgentCLIReviewerConfig(name="claude", cli_path="uv run claude")]
        )
        claude = next(r for r in oc.agent_cli_reviewers if r.name == "claude")
        assert claude.model == "sonnet"
        assert claude.build_invocation("p")[:2] == ["--model", "sonnet"]

    def test_an_explicit_empty_model_passes_no_flag(self) -> None:
        """The escape hatch for a wrapper that rejects unknown args."""
        oc = OperatorConfig(agent_cli_reviewers=[AgentCLIReviewerConfig(name="claude", model="")])
        claude = next(r for r in oc.agent_cli_reviewers if r.name == "claude")
        assert claude.model == ""
        assert claude.build_invocation("p") == ["-p", "p"]

    def test_legacy_claude_cli_promoted_and_deduped(self) -> None:
        oc = OperatorConfig(
            claude_cli=ClaudeCLIConfig(enabled=True, cli_path="claude", model="opus")
        )
        claude_entries = [r for r in oc.agent_cli_reviewers if r.name == "claude"]
        assert len(claude_entries) == 1  # promoted, not doubled
        promoted = claude_entries[0]
        assert promoted.enabled is True
        assert promoted.model == "opus"
        assert promoted.prompt_mode == "flag"
        assert promoted.prompt_arg == "-p"

    def test_user_supplied_list_still_gets_builtins(self) -> None:
        oc = OperatorConfig(
            agent_cli_reviewers=[AgentCLIReviewerConfig(name="mycorp-agent", cli_path="mc")]
        )
        names = {r.name for r in oc.agent_cli_reviewers}
        assert names == {"mycorp-agent", "claude", "codex", "pi", "cursor-agent"}

    def test_disabled_builtin_respected_not_reseeded(self) -> None:
        oc = OperatorConfig(
            agent_cli_reviewers=[AgentCLIReviewerConfig(name="codex", enabled=False)]
        )
        codex_entries = [r for r in oc.agent_cli_reviewers if r.name == "codex"]
        assert len(codex_entries) == 1
        assert codex_entries[0].enabled is False

    def test_explicit_legacy_disable_survives_and_does_not_autorun(self) -> None:
        # H1 regression: `claude_cli: {enabled: false}` must stay disabled and
        # NOT auto-run even when the claude binary is on PATH.
        oc = OperatorConfig(claude_cli=ClaudeCLIConfig(enabled=False))
        claude = next(r for r in oc.agent_cli_reviewers if r.name == "claude")
        assert claude.enabled is False
        with patch("franktheunicorn.worker.runner.shutil.which", return_value="/usr/bin/claude"):
            resolved = resolve_agent_cli_reviewers(oc)
        assert "claude" not in {r.name for r in resolved}

    def test_no_legacy_block_leaves_claude_auto(self) -> None:
        # Never configuring claude_cli keeps the seed at "auto" (auto-detect).
        oc = OperatorConfig()
        claude = next(r for r in oc.agent_cli_reviewers if r.name == "claude")
        assert claude.enabled == "auto"
