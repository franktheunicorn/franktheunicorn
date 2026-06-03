"""Agentic review tools: structured LLM input → safe argv, run in a sandbox.

The Claude backend exposes these tools to the model during review. Each tool
maps a structured (JSON-schema-validated) input into an **argv list** that runs
inside a hardened container via a :class:`ToolRunner`. Model-supplied values
only ever appear as individual argv elements (after ``--`` where supported) —
they are never interpolated into a shell string, so a pattern like
``"; rm -rf /"`` stays a single inert argument.

This module has no Docker dependency: it depends only on the structural
:class:`ToolRunner` protocol, implemented by ``worker.tool_sandbox.ToolSandbox``.
That keeps the ``review`` package importable from the web container.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from franktheunicorn.config.models import AgentToolsConfig
    from franktheunicorn.worker.tool_sandbox import ToolCommandResult

logger = logging.getLogger(__name__)

_MAX_PATTERN_LEN = 1000
_MAX_PATH_LEN = 4096
_GREP_MAX_COUNT = 200
_READ_DEFAULT_WINDOW = 400
_SYMBOLS_CAP = 300


class ToolRunner(Protocol):
    """What a tool needs to execute. Implemented by ``worker.ToolSandbox``."""

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str = "/workspace",
        timeout: int | None = None,
    ) -> ToolCommandResult: ...

    def tool_available(self, binary: str) -> bool: ...


# --- input validation -------------------------------------------------------


def _validate_rel_path(raw: object, *, default: str | None = None) -> str:
    """Validate a model-supplied path: relative, no traversal, length-capped."""
    if raw is None and default is not None:
        return default
    if not isinstance(raw, str) or not raw.strip():
        msg = "path must be a non-empty string"
        raise ValueError(msg)
    path = raw.strip()
    if len(path) > _MAX_PATH_LEN:
        msg = "path is too long"
        raise ValueError(msg)
    if path.startswith("/"):
        msg = "path must be relative to the repo root (no leading '/')"
        raise ValueError(msg)
    if path.startswith("-"):
        msg = "path must not start with '-'"
        raise ValueError(msg)
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        msg = "path must not contain '..' segments"
        raise ValueError(msg)
    return path


def _validate_pattern(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        msg = "pattern must be a non-empty string"
        raise ValueError(msg)
    if len(raw) > _MAX_PATTERN_LEN:
        msg = "pattern is too long"
        raise ValueError(msg)
    return raw


def _validate_line(raw: object, name: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        msg = f"{name} must be an integer"
        raise ValueError(msg)
    if raw < 1:
        msg = f"{name} must be >= 1"
        raise ValueError(msg)
    return raw


# --- result formatting -------------------------------------------------------


def _format_generic(result: ToolCommandResult) -> str:
    if result.timed_out:
        return f"[timed out]\n{result.stderr}".strip()
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    body = "\n".join(parts) or "(no output)"
    return f"exit_code={result.exit_code}\n{body}"


def _format_symbols(result: ToolCommandResult) -> str:
    if result.timed_out:
        return f"[timed out]\n{result.stderr}".strip()
    if result.exit_code != 0 and not result.stdout:
        return _format_generic(result)
    by_kind: dict[str, list[str]] = {}
    count = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("_type") != "tag":
            continue
        name = obj.get("name", "")
        kind = obj.get("kind", "symbol")
        lineno = obj.get("line", "?")
        by_kind.setdefault(kind, []).append(f"{name} (L{lineno})")
        count += 1
        if count >= _SYMBOLS_CAP:
            break
    if not by_kind:
        return "(no symbols found)"
    out: list[str] = []
    for kind in sorted(by_kind):
        out.append(f"{kind}:")
        out.extend(f"  {entry}" for entry in by_kind[kind])
    if count >= _SYMBOLS_CAP:
        out.append(f"... (capped at {_SYMBOLS_CAP} symbols)")
    return "\n".join(out)


# --- tool definition ---------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """A tool the model may call. ``build_argv`` validates input + builds argv."""

    name: str
    description: str
    input_schema: dict[str, Any]
    binary: str  # required binary for availability checks ("" = always present)
    build_argv: Callable[[dict[str, Any]], list[str]]
    format_result: Callable[[ToolCommandResult], str] = _format_generic

    def to_anthropic_spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# --- argv builders (closures bind the resolved binary) -----------------------


def _grep_builder(binary: str) -> Callable[[dict[str, Any]], list[str]]:
    def build(inp: dict[str, Any]) -> list[str]:
        pattern = _validate_pattern(inp.get("pattern"))
        path = _validate_rel_path(inp.get("path"), default=".")
        if binary == "rg":
            return [
                "rg",
                "--no-heading",
                "--line-number",
                "--color",
                "never",
                "--max-count",
                str(_GREP_MAX_COUNT),
                "--",
                pattern,
                path,
            ]
        return ["grep", "-rnI", "--", pattern, path]

    return build


def _find_builder(binary: str) -> Callable[[dict[str, Any]], list[str]]:
    def build(inp: dict[str, Any]) -> list[str]:
        pattern = _validate_pattern(inp.get("pattern"))
        path = _validate_rel_path(inp.get("path"), default=".")
        if binary == "fd":
            return ["fd", "--type", "f", "--glob", "--", pattern, path]
        return ["find", path, "-type", "f", "-name", pattern]

    return build


def _read_file_builder(inp: dict[str, Any]) -> list[str]:
    path = _validate_rel_path(inp.get("path"))
    start = _validate_line(inp.get("start_line"), "start_line")
    end = _validate_line(inp.get("end_line"), "end_line")
    if start is None and end is None:
        return ["cat", "--", path]
    start = start or 1
    if end is None:
        end = start + _READ_DEFAULT_WINDOW
    if end < start:
        msg = "end_line must be >= start_line"
        raise ValueError(msg)
    return ["sed", "-n", f"{start},{end}p", "--", path]


def _symbols_builder(inp: dict[str, Any]) -> list[str]:
    path = _validate_rel_path(inp.get("path"))
    return ["ctags", "--output-format=json", "--fields=+n", "-f", "-", "--", path]


def _command_builder(command: str) -> Callable[[dict[str, Any]], list[str]]:
    # ``command`` comes from trusted operator config, never the model.
    def build(_inp: dict[str, Any]) -> list[str]:
        return ["sh", "-c", command]

    return build


# --- registry ----------------------------------------------------------------

_PATH_PROP = {"type": "string", "description": "Path relative to the repo root."}
_OPT_PATH_PROP = {
    "type": "string",
    "description": "Path relative to the repo root (default: whole repo).",
}


def build_tool_registry(
    cfg: AgentToolsConfig,
    runner: ToolRunner,
    *,
    test_command: str | None = None,
) -> dict[str, Tool]:
    """Return enabled tools whose required binary is available in the sandbox.

    Tools whose binary is missing are silently dropped (logged at INFO) so the
    model is never offered a tool it cannot run. Where a richer tool is absent
    (``rg``/``fd``) a POSIX fallback (``grep``/``find``) is used instead.
    """
    requested = set(cfg.tools)
    registry: dict[str, Tool] = {}

    if "grep" in requested:
        gbin = "rg" if runner.tool_available("rg") else "grep"
        registry["grep"] = Tool(
            name="grep",
            description=(
                "Search file contents by regular expression across the checked-out "
                "repository. Returns matching lines with file and line number."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex to search for."},
                    "path": _OPT_PATH_PROP,
                },
                "required": ["pattern"],
            },
            binary=gbin,
            build_argv=_grep_builder(gbin),
        )

    if "find_files" in requested:
        fbin = "fd" if runner.tool_available("fd") else "find"
        registry["find_files"] = Tool(
            name="find_files",
            description="Locate files by name/glob pattern under a directory.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Filename glob, e.g. '*.py' or 'test_*.py'.",
                    },
                    "path": _OPT_PATH_PROP,
                },
                "required": ["pattern"],
            },
            binary=fbin,
            build_argv=_find_builder(fbin),
        )

    if "read_file" in requested and runner.tool_available("cat"):
        registry["read_file"] = Tool(
            name="read_file",
            description=(
                "Read a file from the checkout, optionally a line range. Use to "
                "inspect code beyond the diff."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": _PATH_PROP,
                    "start_line": {"type": "integer", "description": "First line (1-based)."},
                    "end_line": {"type": "integer", "description": "Last line (inclusive)."},
                },
                "required": ["path"],
            },
            binary="cat",
            build_argv=_read_file_builder,
        )

    if "list_symbols" in requested:
        if runner.tool_available("ctags"):
            registry["list_symbols"] = Tool(
                name="list_symbols",
                description=(
                    "List the symbols (functions, classes, methods, …) defined in a "
                    "file, with line numbers, via ctags."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"path": _PATH_PROP},
                    "required": ["path"],
                },
                binary="ctags",
                build_argv=_symbols_builder,
                format_result=_format_symbols,
            )
        else:
            logger.info("list_symbols dropped: 'ctags' not available in tool image.")

    if "compile_build" in requested and cfg.enable_compile and cfg.build_command:
        registry["compile_build"] = Tool(
            name="compile_build",
            description=(
                "Compile/build/type-check the project using its configured command "
                "and return diagnostics. Takes no arguments."
            ),
            input_schema={"type": "object", "properties": {}},
            binary="",
            build_argv=_command_builder(cfg.build_command),
        )

    if "run_tests" in requested and cfg.enable_run_tests and test_command:
        registry["run_tests"] = Tool(
            name="run_tests",
            description=(
                "Run the project's test suite using its configured command and "
                "return the output. Takes no arguments."
            ),
            input_schema={"type": "object", "properties": {}},
            binary="",
            build_argv=_command_builder(test_command),
        )

    return registry


def anthropic_tool_specs(registry: dict[str, Tool]) -> list[dict[str, Any]]:
    """Return the Anthropic ``tools=`` specs for every tool in the registry."""
    return [tool.to_anthropic_spec() for tool in registry.values()]


def dispatch_tool_use(
    registry: dict[str, Tool],
    runner: ToolRunner,
    name: str,
    tool_input: dict[str, Any] | None,
) -> str:
    """Execute a tool_use block, returning result text. Never raises.

    Unknown tools, invalid input, and execution errors all yield a structured
    error string so the agentic loop can recover and continue.
    """
    tool = registry.get(name)
    if tool is None:
        return f"[error] unknown tool '{name}'"
    try:
        argv = tool.build_argv(tool_input or {})
    except ValueError as exc:
        return f"[error] invalid input for '{name}': {exc}"
    except Exception as exc:
        logger.debug("Unexpected error building argv for %s", name, exc_info=True)
        return f"[error] could not build command for '{name}': {exc}"
    result = runner.exec(argv)
    try:
        return tool.format_result(result)
    except Exception:
        logger.debug("Failed to format result for %s", name, exc_info=True)
        return _format_generic(result)


__all__ = [
    "Tool",
    "ToolRunner",
    "anthropic_tool_specs",
    "build_tool_registry",
    "dispatch_tool_use",
]
