"""Tests for the Claude backend's agentic tool-use loop."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from franktheunicorn.config.models import AgentToolsConfig, LLMBackendConfig
from franktheunicorn.review.agent_tools import anthropic_tool_specs, build_tool_registry
from franktheunicorn.review.backends.claude_backend import ClaudeBackend
from franktheunicorn.worker.tool_sandbox import ToolCommandResult
from tests.conftest import make_pr_context

_DIFF = "diff --git a/a.py b/a.py\n+x = 1\n"
_FINAL_JSON = json.dumps(
    {
        "overall_vibe": "looks fine",
        "findings": [
            {"file_path": "a.py", "line_number": 1, "title": "T", "body": "B", "severity": "low"}
        ],
    }
)


class FakeRunner:
    def __init__(self, available: set[str]):
        self._available = available
        self.calls: list[list[str]] = []

    def exec(self, argv, *, cwd="/workspace", timeout=None):
        self.calls.append(argv)
        return ToolCommandResult(0, "search result", "", False)

    def tool_available(self, binary: str) -> bool:
        return binary in self._available


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, inp: dict, block_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=block_id)


def _response(content: list, stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _attach(backend: ClaudeBackend, runner: FakeRunner, cfg: AgentToolsConfig) -> None:
    registry = build_tool_registry(cfg, runner)
    specs = anthropic_tool_specs(registry)
    backend.attach_tools(runner, registry, specs, cfg)


@patch.dict("os.environ", {"TEST_ANTHROPIC_KEY": "sk-test"})
class TestAgenticLoop:
    def _backend(self) -> ClaudeBackend:
        return ClaudeBackend(LLMBackendConfig(provider="claude", api_key_env="TEST_ANTHROPIC_KEY"))

    def test_runs_tool_then_returns_findings(self) -> None:
        backend = self._backend()
        runner = FakeRunner(available={"rg", "cat"})
        _attach(backend, runner, AgentToolsConfig(enabled=True))

        tool_turn = _response([_tool_use_block("grep", {"pattern": "x"}, "t1")], "tool_use")
        final_turn = _response([_text_block(_FINAL_JSON)], "end_turn")

        with patch("anthropic.Anthropic") as mock_cls:
            create = mock_cls.return_value.messages.create
            create.side_effect = [tool_turn, final_turn]
            result = backend.generate_review(_DIFF, make_pr_context())

        assert len(result.findings) == 1
        assert result.overall_vibe == "looks fine"
        # The tool was actually executed in the sandbox.
        assert len(runner.calls) == 1
        # Two model turns; tokens accumulate across both.
        assert create.call_count == 2
        assert backend._last_tokens_in == 20
        assert backend._last_tokens_out == 10
        # Tools were offered to the model.
        assert "tools" in create.call_args_list[0].kwargs

    def test_max_iterations_terminates_runaway_loop(self) -> None:
        backend = self._backend()
        runner = FakeRunner(available={"rg", "cat"})
        _attach(backend, runner, AgentToolsConfig(enabled=True, max_iterations=3))

        # The model never stops requesting tools.
        looping = _response([_tool_use_block("grep", {"pattern": "x"}, "t1")], "tool_use")

        with patch("anthropic.Anthropic") as mock_cls:
            create = mock_cls.return_value.messages.create
            create.return_value = looping
            result = backend.generate_review(_DIFF, make_pr_context())

        assert create.call_count == 3  # bounded by max_iterations
        assert result.findings == []  # no final JSON ever emitted

    def test_unknown_tool_recovers_and_continues(self) -> None:
        backend = self._backend()
        runner = FakeRunner(available={"rg", "cat"})
        _attach(backend, runner, AgentToolsConfig(enabled=True))

        bad_turn = _response([_tool_use_block("does_not_exist", {}, "t1")], "tool_use")
        final_turn = _response([_text_block(_FINAL_JSON)], "end_turn")

        with patch("anthropic.Anthropic") as mock_cls:
            create = mock_cls.return_value.messages.create
            create.side_effect = [bad_turn, final_turn]
            result = backend.generate_review(_DIFF, make_pr_context())

        # Loop recovered from the bad tool and still produced findings.
        assert len(result.findings) == 1
        assert create.call_count == 2


@patch.dict("os.environ", {"TEST_ANTHROPIC_KEY": "sk-test"})
class TestNoToolsPathUnchanged:
    def test_one_shot_when_no_tools_attached(self) -> None:
        backend = ClaudeBackend(
            LLMBackendConfig(provider="claude", api_key_env="TEST_ANTHROPIC_KEY")
        )
        single = _response([_text_block(_FINAL_JSON)], "end_turn")

        with patch("anthropic.Anthropic") as mock_cls:
            create = mock_cls.return_value.messages.create
            create.return_value = single
            result = backend.generate_review(_DIFF, make_pr_context())

        assert len(result.findings) == 1
        # One-shot: a single call with NO tools parameter.
        assert create.call_count == 1
        assert "tools" not in create.call_args.kwargs
