"""Tests for the agentic review tool registry (review/agent_tools.py)."""

from __future__ import annotations

import json

import pytest

from franktheunicorn.config.models import AgentToolsConfig
from franktheunicorn.review.agent_tools import (
    _format_symbols,
    _validate_rel_path,
    anthropic_tool_specs,
    build_tool_registry,
    dispatch_tool_use,
)
from franktheunicorn.worker.tool_sandbox import ToolCommandResult


class FakeRunner:
    """In-memory ToolRunner: records argv, returns a canned result."""

    def __init__(self, available: set[str] | None = None, result: ToolCommandResult | None = None):
        self._available = available if available is not None else set()
        self._result = result or ToolCommandResult(0, "ok", "", False)
        self.calls: list[list[str]] = []

    def exec(self, argv, *, cwd="/workspace", timeout=None):
        self.calls.append(argv)
        return self._result

    def tool_available(self, binary: str) -> bool:
        return binary in self._available


_ALL = AgentToolsConfig(
    enabled=True,
    tools=["grep", "find_files", "read_file", "list_symbols", "compile_build", "run_tests"],
    enable_compile=True,
    build_command="make build",
    enable_run_tests=True,
)


class TestPathValidation:
    def test_rejects_absolute(self) -> None:
        with pytest.raises(ValueError, match="relative"):
            _validate_rel_path("/etc/passwd")

    def test_rejects_traversal(self) -> None:
        with pytest.raises(ValueError, match=r"\.\."):
            _validate_rel_path("../../secret")

    def test_rejects_leading_dash(self) -> None:
        with pytest.raises(ValueError, match="-"):
            _validate_rel_path("-rf")

    def test_default_when_none(self) -> None:
        assert _validate_rel_path(None, default=".") == "."

    def test_accepts_normal_relative(self) -> None:
        assert _validate_rel_path("src/main.py") == "src/main.py"

    def test_rejects_too_long_path(self) -> None:
        with pytest.raises(ValueError, match="too long"):
            _validate_rel_path("a/" * 3000)

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            _validate_rel_path("   ")


class TestGrepTool:
    def test_uses_ripgrep_when_available(self) -> None:
        runner = FakeRunner(available={"rg", "cat"})
        registry = build_tool_registry(_ALL, runner)
        argv = registry["grep"].build_argv({"pattern": "needle", "path": "src"})
        assert argv[0] == "rg"
        # Pattern and path come after `--` so flags can't be injected.
        assert "--" in argv
        assert argv[-2:] == ["needle", "src"]

    def test_falls_back_to_grep(self) -> None:
        runner = FakeRunner(available={"cat"})  # no rg
        registry = build_tool_registry(_ALL, runner)
        argv = registry["grep"].build_argv({"pattern": "x"})
        assert argv[0] == "grep"

    def test_injection_pattern_stays_single_arg(self) -> None:
        runner = FakeRunner(available={"rg", "cat"})
        registry = build_tool_registry(_ALL, runner)
        argv = registry["grep"].build_argv({"pattern": "; rm -rf /"})
        # The whole malicious string is one argv element after `--`.
        assert "; rm -rf /" in argv
        sep = argv.index("--")
        assert argv[sep + 1] == "; rm -rf /"

    def test_missing_pattern_raises(self) -> None:
        runner = FakeRunner(available={"rg", "cat"})
        registry = build_tool_registry(_ALL, runner)
        with pytest.raises(ValueError, match="pattern"):
            registry["grep"].build_argv({})

    def test_too_long_pattern_raises(self) -> None:
        runner = FakeRunner(available={"rg", "cat"})
        registry = build_tool_registry(_ALL, runner)
        with pytest.raises(ValueError, match="too long"):
            registry["grep"].build_argv({"pattern": "x" * 2000})


class TestFindTool:
    def test_uses_fd_when_available(self) -> None:
        runner = FakeRunner(available={"fd", "cat"})
        registry = build_tool_registry(_ALL, runner)
        argv = registry["find_files"].build_argv({"pattern": "*.py"})
        assert argv[0] == "fd"

    def test_falls_back_to_find(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner)
        argv = registry["find_files"].build_argv({"pattern": "*.py", "path": "src"})
        assert argv[0] == "find"
        assert argv[1] == "src"


class TestReadFileTool:
    def test_full_file_uses_cat(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner)
        argv = registry["read_file"].build_argv({"path": "a.py"})
        assert argv == ["cat", "--", "a.py"]

    def test_range_uses_sed(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner)
        argv = registry["read_file"].build_argv({"path": "a.py", "start_line": 10, "end_line": 20})
        assert argv[0] == "sed"
        assert "10,20p" in argv

    def test_dropped_when_cat_missing(self) -> None:
        runner = FakeRunner(available=set())
        registry = build_tool_registry(_ALL, runner)
        assert "read_file" not in registry

    def test_end_before_start_raises(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner)
        with pytest.raises(ValueError, match="end_line"):
            registry["read_file"].build_argv({"path": "a.py", "start_line": 20, "end_line": 5})

    def test_non_integer_line_raises(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner)
        with pytest.raises(ValueError, match="integer"):
            registry["read_file"].build_argv({"path": "a.py", "start_line": "ten"})

    def test_line_below_one_raises(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner)
        with pytest.raises(ValueError, match=">= 1"):
            registry["read_file"].build_argv({"path": "a.py", "start_line": 0})

    def test_only_start_line_defaults_window(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner)
        argv = registry["read_file"].build_argv({"path": "a.py", "start_line": 5})
        assert argv[0] == "sed"
        assert "5,405p" in argv


class TestSymbolsTool:
    def test_present_when_ctags_available(self) -> None:
        runner = FakeRunner(available={"ctags", "cat"})
        registry = build_tool_registry(_ALL, runner)
        assert "list_symbols" in registry
        argv = registry["list_symbols"].build_argv({"path": "a.py"})
        assert argv[0] == "ctags"

    def test_dropped_when_ctags_missing(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner)
        assert "list_symbols" not in registry

    def test_format_symbols_parses_ctags_json(self) -> None:
        lines = [
            json.dumps({"_type": "tag", "name": "foo", "kind": "function", "line": 3}),
            json.dumps({"_type": "tag", "name": "Bar", "kind": "class", "line": 10}),
            "not json",
        ]
        result = ToolCommandResult(0, "\n".join(lines), "", False)
        out = _format_symbols(result)
        assert "function:" in out
        assert "foo (L3)" in out
        assert "Bar (L10)" in out

    def test_format_symbols_empty(self) -> None:
        result = ToolCommandResult(0, "", "", False)
        assert "no symbols" in _format_symbols(result)


class TestCompileAndRunTests:
    def test_compile_present_only_when_enabled(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner, test_command="pytest -q")
        argv = registry["compile_build"].build_argv({})
        assert argv == ["sh", "-c", "make build"]

    def test_compile_absent_without_flag(self) -> None:
        runner = FakeRunner(available={"cat"})
        cfg = AgentToolsConfig(enabled=True, tools=["compile_build"])
        registry = build_tool_registry(cfg, runner)
        assert "compile_build" not in registry

    def test_run_tests_uses_test_command(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner, test_command="pytest -q")
        argv = registry["run_tests"].build_argv({})
        assert argv == ["sh", "-c", "pytest -q"]

    def test_run_tests_absent_without_command(self) -> None:
        runner = FakeRunner(available={"cat"})
        registry = build_tool_registry(_ALL, runner, test_command=None)
        assert "run_tests" not in registry


class TestDispatchAndSpecs:
    def test_unknown_tool_returns_error(self) -> None:
        runner = FakeRunner(available={"cat"})
        out = dispatch_tool_use({}, runner, "nope", {})
        assert out.startswith("[error]")
        assert "unknown tool" in out

    def test_invalid_input_returns_error_not_raise(self) -> None:
        runner = FakeRunner(available={"rg", "cat"})
        registry = build_tool_registry(_ALL, runner)
        out = dispatch_tool_use(registry, runner, "read_file", {"path": "/abs"})
        assert out.startswith("[error]")
        # Runner never invoked on invalid input.
        assert runner.calls == []

    def test_valid_call_executes_and_formats(self) -> None:
        runner = FakeRunner(
            available={"rg", "cat"}, result=ToolCommandResult(0, "match line", "", False)
        )
        registry = build_tool_registry(_ALL, runner)
        out = dispatch_tool_use(registry, runner, "grep", {"pattern": "x"})
        assert "match line" in out
        assert len(runner.calls) == 1

    def test_timed_out_result_formatted(self) -> None:
        runner = FakeRunner(
            available={"rg", "cat"},
            result=ToolCommandResult(-1, "", "timed out after 20s", True),
        )
        registry = build_tool_registry(_ALL, runner)
        out = dispatch_tool_use(registry, runner, "grep", {"pattern": "x"})
        assert "timed out" in out.lower()

    def test_anthropic_specs_shape(self) -> None:
        runner = FakeRunner(available={"rg", "fd", "cat", "ctags"})
        registry = build_tool_registry(_ALL, runner, test_command="pytest")
        specs = anthropic_tool_specs(registry)
        assert all({"name", "description", "input_schema"} <= set(s) for s in specs)
        names = {s["name"] for s in specs}
        assert "grep" in names
