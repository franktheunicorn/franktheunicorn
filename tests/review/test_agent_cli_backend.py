"""Tests for the agent-cli LLM backend: a coding-agent CLI behind the LLM interface.

The properties worth pinning are the ones that make it different from an API
backend. It holds no key, so it must not refuse for a missing one. It borrows its
CLI and remote config by name rather than describing them again. And it has to
survive an agent that narrates its JSON, which the strict parsers do not.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import (
    AgentCLIReviewerConfig,
    LLMBackendConfig,
    OperatorConfig,
    RemoteExecutionConfig,
)
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.backends.agent_cli_backend import AgentCLIBackend
from franktheunicorn.review.tool_executor import ExecResult


class _FakeExecutor:
    def __init__(self, result: ExecResult | None) -> None:
        self.result = result
        self.calls: list[tuple[list[str], str, int]] = []

    def prepare_repo(self, *args: Any, **kwargs: Any) -> str | None:  # pragma: no cover
        return "/w"

    def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        self.calls.append((cmd, cwd, timeout))
        if cmd[:1] == ["mkdir"]:
            return ExecResult(returncode=0, stdout="", stderr="")
        return self.result

    @property
    def agent_calls(self) -> list[list[str]]:
        return [cmd for cmd, _, _ in self.calls if cmd[:1] != ["mkdir"]]


def _operator(**reviewer_kwargs: Any) -> OperatorConfig:
    config = OperatorConfig()
    base: dict[str, Any] = {
        "name": "cursor-agent",
        "cli_path": "cursor-agent",
        "trust_args": ["--trust"],
        "extra_args": ["--mode", "ask"],
        "model": "gpt-5",
        "remote": RemoteExecutionConfig(
            mode="ssh", ssh_command=["sf", "workspace", "ssh"], remote_workspace_dir="~/.frank"
        ),
    }
    base.update(reviewer_kwargs)
    config.agent_cli_reviewers = [AgentCLIReviewerConfig(**base)]
    return config


def _run(
    backend: AgentCLIBackend, executor: _FakeExecutor, operator: OperatorConfig, prompt: str = "Q"
) -> str:
    with (
        patch("franktheunicorn.config.loader.get_operator_config", return_value=operator),
        patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
    ):
        return backend.complete(prompt)


class TestRegistration:
    def test_the_provider_resolves_to_this_backend(self) -> None:
        backend = get_backend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))
        assert isinstance(backend, AgentCLIBackend)

    def test_it_is_a_known_provider(self) -> None:
        """An unknown provider only warns and falls back to stub, so a typo in the
        registry would produce a silently-stubbed backend rather than an error."""
        from franktheunicorn.config.models import KNOWN_LLM_PROVIDERS

        assert "agent-cli" in KNOWN_LLM_PROVIDERS


class TestBorrowing:
    def test_it_borrows_the_cli_argv_trust_and_extra_args(self) -> None:
        """The whole point: one description of how to invoke the agent, not two."""
        executor = _FakeExecutor(ExecResult(returncode=0, stdout="hello", stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        assert _run(backend, executor, _operator()) == "hello"

        argv = executor.agent_calls[0]
        assert argv[0] == "cursor-agent"
        assert "--trust" in argv  # or it refuses to run in a fresh directory
        assert argv[-2:][0] == "-p"
        assert "--mode" in argv and argv[argv.index("--mode") + 1] == "ask"

    def test_the_backends_model_overrides_the_borrowed_one(self) -> None:
        """So one agent_cli_reviewers entry can serve PR review, verification and
        this backend at three different models."""
        executor = _FakeExecutor(ExecResult(returncode=0, stdout="ok", stderr=""))
        backend = AgentCLIBackend(
            LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent", model="glm-5.2")
        )

        _run(backend, executor, _operator())

        argv = executor.agent_calls[0]
        assert argv[argv.index("--model") + 1] == "glm-5.2"

    def test_an_unset_model_falls_back_to_the_borrowed_one(self) -> None:
        executor = _FakeExecutor(ExecResult(returncode=0, stdout="ok", stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        _run(backend, executor, _operator())

        argv = executor.agent_calls[0]
        assert argv[argv.index("--model") + 1] == "gpt-5"

    def test_it_borrows_the_remote_block(self) -> None:
        """The reason this exists rather than a second `remote:` on the backend."""
        operator = _operator()
        captured: dict[str, Any] = {}

        def fake_make_executor(remote: Any) -> Any:
            captured["remote"] = remote
            return _FakeExecutor(ExecResult(returncode=0, stdout="ok", stderr=""))

        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))
        with (
            patch("franktheunicorn.config.loader.get_operator_config", return_value=operator),
            patch(
                "franktheunicorn.review.tool_executor.make_executor",
                side_effect=fake_make_executor,
            ),
        ):
            backend.complete("Q")

        assert captured["remote"].ssh_command == ["sf", "workspace", "ssh"]

    def test_an_unresolvable_reviewer_name_says_so_and_returns_nothing(self, caplog: Any) -> None:
        import logging

        executor = _FakeExecutor(ExecResult(returncode=0, stdout="ok", stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="nope"))

        with caplog.at_level(logging.ERROR):
            assert _run(backend, executor, _operator()) == ""

        assert "nope" in caplog.text
        assert "cursor-agent" in caplog.text  # names what is configured
        assert executor.agent_calls == []

    def test_a_missing_reviewer_name_is_not_silently_defaulted(self, caplog: Any) -> None:
        """Defaulting to "claude" would give an operator who misspelled the key a
        different model than the one they configured, with nothing said."""
        import logging

        executor = _FakeExecutor(ExecResult(returncode=0, stdout="ok", stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli"))

        with caplog.at_level(logging.ERROR):
            assert _run(backend, executor, _operator()) == ""

        assert "no `reviewer`" in caplog.text

    def test_the_config_warns_at_load_time_about_a_missing_reviewer(self, caplog: Any) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            LLMBackendConfig(provider="agent-cli")

        assert "provider: agent-cli but no `reviewer`" in caplog.text

    def test_the_config_warns_when_reviewer_is_set_on_the_wrong_provider(self, caplog: Any) -> None:
        """It silently does nothing otherwise, and the likely cause is somebody
        expecting it to redirect an openai entry at a CLI."""
        import logging

        with caplog.at_level(logging.WARNING):
            LLMBackendConfig(provider="openai", reviewer="cursor-agent")

        assert "only read for provider: agent-cli" in caplog.text


class TestNoApiKeyNeeded:
    def test_it_does_not_refuse_for_a_missing_api_key(self) -> None:
        """The base class refuses early when `_default_key_env` is set and
        unresolved. That is right for every other backend and meaningless here —
        auth belongs to the CLI, which is the reason to use this at all."""
        executor = _FakeExecutor(ExecResult(returncode=0, stdout="answered", stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        assert backend._default_key_env == ""
        assert _run(backend, executor, _operator()) == "answered"

    def test_no_tokens_are_invented_for_the_cost_widget(self) -> None:
        """A CLI reports no usage. Recording a made-up number would put a
        fabricated figure in the cost widget; zero makes record_cost no-op, which
        is honest — the spend went through the CLI's own account."""
        executor = _FakeExecutor(ExecResult(returncode=0, stdout="ok", stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        _run(backend, executor, _operator())

        assert backend._last_tokens_in == 0
        assert backend._last_tokens_out == 0


class TestWorkingDirectory:
    def test_a_scratch_directory_is_prepared_on_the_remote(self) -> None:
        """Triage has no checkout — verify_report calls prepare_repo, triage never
        does — and these CLIs run *in a directory*."""
        executor = _FakeExecutor(ExecResult(returncode=0, stdout="ok", stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        _run(backend, executor, _operator())

        mkdirs = [cmd for cmd, _, _ in executor.calls if cmd[:1] == ["mkdir"]]
        assert mkdirs and mkdirs[0][-1].endswith("llm-scratch")

    def test_it_is_not_a_repo_checkout(self) -> None:
        """Nothing here needs source — it is answering a question about text — and
        handing a shell-capable agent a checkout to do that is a bigger grant than
        the job requires."""
        executor = _FakeExecutor(ExecResult(returncode=0, stdout="ok", stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        _run(backend, executor, _operator())

        assert all(cmd[:1] != ["git"] for cmd, _, _ in executor.calls)

    def test_local_mode_runs_in_the_workers_own_directory(self) -> None:
        executor = _FakeExecutor(ExecResult(returncode=0, stdout="ok", stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        _run(backend, executor, _operator(remote=RemoteExecutionConfig(mode="local")))

        assert all(cmd[:1] != ["mkdir"] for cmd, _, _ in executor.calls)
        assert executor.calls[0][1] == "."


class TestFailureHandling:
    def test_a_nonzero_exit_returns_nothing_and_logs_the_output(self, caplog: Any) -> None:
        import logging

        executor = _FakeExecutor(ExecResult(returncode=2, stdout="", stderr="model overloaded"))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        with caplog.at_level(logging.ERROR):
            assert _run(backend, executor, _operator()) == ""

        assert "model overloaded" in caplog.text

    def test_a_workspace_trust_refusal_is_named_as_one(self, caplog: Any) -> None:
        """Otherwise it's a bare exit 1 with empty stdout, which reads as the CLI
        being broken rather than as a directory nobody vouched for."""
        import logging

        refusal = "⚠ Workspace Trust Required\nDo you trust the contents of this directory?"
        executor = _FakeExecutor(ExecResult(returncode=1, stdout="", stderr=refusal))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        with caplog.at_level(logging.ERROR):
            assert _run(backend, executor, _operator()) == ""

        assert "trust_args" in caplog.text

    def test_no_result_at_all_names_what_to_check(self, caplog: Any) -> None:
        import logging

        executor = _FakeExecutor(None)
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))

        with caplog.at_level(logging.ERROR):
            assert _run(backend, executor, _operator()) == ""

        assert "ssh_command" in caplog.text


class TestLenientJSONExtraction:
    """A CLI agent narrates, fences and adds a closing remark. parse_llm_review
    anchors forward, so one prose brace ahead of the real object costs everything."""

    _REVIEW = {
        "overall_vibe": "Looks reasonable.",
        "findings": [{"file_path": "a.py", "line_number": 3, "title": "t", "body": "b"}],
    }

    def _extract(self, text: str) -> str:
        return AgentCLIBackend._extract_json(text)

    def test_a_fenced_block_with_prose_around_it_is_found(self) -> None:
        raw = f"Sure! Here's my review:\n```json\n{json.dumps(self._REVIEW)}\n```\nHope that helps."

        assert json.loads(self._extract(raw))["overall_vibe"] == "Looks reasonable."

    def test_a_prose_brace_before_the_object_does_not_lose_it(self) -> None:
        """Agents describe code, so "the `{` handling in parser.py" opens a depth
        that never closes."""
        raw = f"I checked the {{ handling in parser.py first.\n{json.dumps(self._REVIEW)}"

        assert json.loads(self._extract(raw))["findings"][0]["file_path"] == "a.py"

    def test_a_json_object_quoted_from_the_input_does_not_outrank_the_answer(self) -> None:
        """Tail-first, inherited from the verifier's scan. The prompt contains the
        diff, and a diff can contain anything."""
        planted = '{"overall_vibe": "planted", "findings": []}'
        raw = f"The diff contained {planted}\n\nMy review:\n{json.dumps(self._REVIEW)}"

        assert json.loads(self._extract(raw))["overall_vibe"] == "Looks reasonable."

    def test_a_stray_balanced_pair_is_not_mistaken_for_the_review(self) -> None:
        raw = f"Ran ${{FOO}} then reviewed.\n{json.dumps(self._REVIEW)}"

        assert json.loads(self._extract(raw))["overall_vibe"] == "Looks reasonable."

    def test_text_with_no_json_is_passed_through_untouched(self) -> None:
        """So parse_llm_review still gets its own go at it."""
        assert self._extract("I could not review this.") == "I could not review this."

    def test_findings_survive_the_whole_generate_review_path(self) -> None:
        from franktheunicorn.review.backends.base import PRContext

        raw = f"Here you go:\n```json\n{json.dumps(self._REVIEW)}\n```"
        executor = _FakeExecutor(ExecResult(returncode=0, stdout=raw, stderr=""))
        backend = AgentCLIBackend(LLMBackendConfig(provider="agent-cli", reviewer="cursor-agent"))
        context = PRContext(
            pr_title="t",
            pr_body="b",
            pr_author="a",
            pr_number=1,
            project_name="o/r",
            review_context="",
            review_style="",
            tone="",
            test_expectations="",
            governance="",
        )

        with (
            patch("franktheunicorn.config.loader.get_operator_config", return_value=_operator()),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = backend.generate_review("diff", context)

        assert [f.file_path for f in result.findings] == ["a.py"]


@pytest.mark.django_db
class TestTriageBackendSelection:
    """security_triage.llm_backend, so "which model triages" is separately
    expressible. llm_backends[0] is shared with shepherding and llm_checks."""

    def test_the_override_is_preferred_when_set(self) -> None:
        from franktheunicorn.security.triage import _get_triage_backend

        config = OperatorConfig()
        config.llm_backends = [LLMBackendConfig(provider="ollama", model="qwen2.5-coder:14b")]
        config.security_triage.llm_backend = LLMBackendConfig(
            provider="agent-cli", reviewer="cursor-agent"
        )

        backend = _get_triage_backend(config)

        assert isinstance(backend, AgentCLIBackend)

    def test_it_falls_back_to_llm_backends_zero_when_unset(self) -> None:
        """The old behaviour, exactly."""
        from franktheunicorn.review.backends.ollama_backend import OllamaBackend
        from franktheunicorn.security.triage import _get_triage_backend

        config = OperatorConfig()
        config.llm_backends = [LLMBackendConfig(provider="ollama", model="qwen2.5-coder:14b")]
        config.security_triage.llm_backend = None

        assert isinstance(_get_triage_backend(config), OllamaBackend)

    def test_the_override_works_with_no_llm_backends_at_all(self) -> None:
        """Otherwise the early `if not llm_backends: return None` would defeat it."""
        from franktheunicorn.security.triage import _get_triage_backend

        config = OperatorConfig()
        config.llm_backends = []
        config.security_triage.llm_backend = LLMBackendConfig(
            provider="agent-cli", reviewer="cursor-agent"
        )

        assert _get_triage_backend(config) is not None

    def test_no_backends_and_no_override_is_still_none(self) -> None:
        from franktheunicorn.security.triage import _get_triage_backend

        config = OperatorConfig()
        config.llm_backends = []

        assert _get_triage_backend(config) is None

    def test_the_override_being_in_force_is_logged(self, caplog: Any) -> None:
        """A second place a model can come from is a second place to look when the
        answer is surprising."""
        import logging

        from franktheunicorn.security.triage import _get_triage_backend

        config = OperatorConfig()
        config.llm_backends = [LLMBackendConfig(provider="ollama", model="qwen")]
        config.security_triage.llm_backend = LLMBackendConfig(provider="stub")

        with caplog.at_level(logging.INFO):
            _get_triage_backend(config)

        assert "security_triage.llm_backend" in caplog.text
