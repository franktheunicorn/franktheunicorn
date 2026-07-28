"""Claude Code backend for review generation — no API key required.

Talks to a local Claude Code agent (subscription/CLI-auth, not the Anthropic
API) so Frank can draft reviews, answer RLM ``complete()`` calls, and run
security triage assessments without an API key. Billing happens through the
agent's own authenticated session, so every call this backend makes is
recorded at $0 on Frank's own cost ledger — see the ``"claude-code"`` entry
in ``_COST_PER_MTOK`` in :mod:`franktheunicorn.review.backends.base`.

Two transports, picked by ``config.transport``:

* ``"cli"`` (default) — shells out to the local ``claude`` binary in
  headless prompt mode (``claude -p ... --output-format json``).
* ``"acp"`` — speaks the Agent Client Protocol (JSON-RPC over stdio,
  https://agentclientprotocol.com/) to an ACP agent adapter such as
  ``claude-code-acp``. See :mod:`franktheunicorn.review.backends.acp_transport`
  for the protocol client.

This is distinct from ``review/claude_cli.py`` (the standalone "agent CLI
reviewer", configured under the operator's ``claude_cli:`` block, that runs
``claude`` directly against a repo checkout and creates ``ReviewDraft`` rows
tagged with source ``"claude-cli"``). This module instead implements the
standard :class:`LLMBackend` interface so Claude Code can be selected as an
ordinary ``provider: "claude-code"`` backend everywhere backends are already
pluggable: the review drafter, the RLM broker, and ``metered_call`` sites
like tone guard and security triage.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import tempfile
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.acp_transport import AcpProtocolError, run_acp_prompt
from franktheunicorn.review.backends.base import BaseLLMBackend
from franktheunicorn.review.tool_executor import make_executor

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)

# Tools disabled on every invocation: this backend is used as a pure text
# completion primitive (review JSON, RLM notebook calls, security triage
# prompts) and must never wander the filesystem or shell out on our behalf.
# For the "cli" transport this is enforced via --disallowedTools; for "acp"
# it's enforced by advertising no client capabilities (see acp_transport).
_DISALLOWED_TOOLS = ["Bash", "Edit", "Write", "Read"]

_DEFAULT_CLI_TIMEOUT_SECONDS = 300


class ClaudeCodeBackend(BaseLLMBackend):
    """Review backend that talks to a local Claude Code agent.

    Unlike the SDK-backed backends (:class:`ClaudeBackend` et al.) this
    backend has no Python SDK dependency and needs no API key — it drives
    a Claude Code agent the operator already has installed and logged into,
    either as a CLI subprocess (headless prompt mode: ``claude -p ...
    --output-format json``) or as an ACP agent over JSON-RPC/stdio,
    depending on ``config.transport``.
    """

    _sdk_module = ""
    _default_key_env = ""
    _default_model = ""

    def __init__(self, config: LLMBackendConfig) -> None:
        super().__init__(config)
        self._transport = getattr(config, "transport", "") or "cli"
        self._cli_path = getattr(config, "cli_path", "") or "claude"
        self._acp_command = getattr(config, "acp_command", "") or "claude-code-acp"
        self._cli_timeout = (
            getattr(config, "cli_timeout_seconds", 0) or _DEFAULT_CLI_TIMEOUT_SECONDS
        )
        # BaseLLMBackend.__init__ only probes `_sdk_module` importability,
        # which is irrelevant here since we have no SDK. Re-derive
        # `_sdk_available` from whether the relevant binary is actually on
        # PATH, so the base class's graceful-degradation paths (empty
        # findings, "" completion) kick in the same way they do for a
        # missing SDK package.
        probe_binary = self._acp_binary_argv()[0] if self._transport == "acp" else self._cli_path
        self._sdk_available = shutil.which(probe_binary) is not None
        if not self._sdk_available:
            logger.error(
                "%s binary %r not found on PATH; claude-code backend (transport=%s) disabled.",
                "ACP agent" if self._transport == "acp" else "claude CLI",
                probe_binary,
                self._transport,
            )

    def _acp_binary_argv(self) -> list[str]:
        """Split ``acp_command`` into argv (supports ``"cmd arg1 arg2"``)."""
        parts = shlex.split(self._acp_command) if self._acp_command else []
        return parts or ["claude-code-acp"]

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        if self._transport == "acp":
            return self._call_acp(system_prompt, user_message)
        return self._call_cli(system_prompt, user_message)

    def _call_cli(self, system_prompt: str, user_message: str) -> str:
        argv = [self._cli_path, "-p", user_message, "--output-format", "json"]
        if system_prompt:
            argv += ["--system-prompt", system_prompt]
        if self._model:
            argv += ["--model", self._model]
        argv += ["--disallowedTools", *_DISALLOWED_TOOLS]

        # Run from a neutral directory (not a project checkout) so the CLI
        # has nothing to wander into even if a tool guard is ever bypassed.
        executor = make_executor(None)
        result = executor.run(argv, cwd=tempfile.gettempdir(), timeout=self._cli_timeout)
        if result is None or not result.ok:
            stderr = (
                result.stderr if result is not None else "(no result — CLI missing or timed out)"
            )
            raise RuntimeError(f"claude CLI failed: {stderr}")

        return self._parse_output(result.stdout)

    def _call_acp(self, system_prompt: str, user_message: str) -> str:
        try:
            text, tokens_in, tokens_out = run_acp_prompt(
                self._acp_binary_argv(),
                cwd=tempfile.gettempdir(),
                timeout=float(self._cli_timeout),
                system_prompt=system_prompt,
                user_message=user_message,
            )
        except AcpProtocolError as exc:
            raise RuntimeError(f"ACP agent failed: {exc}") from exc

        self._last_tokens_in = tokens_in
        self._last_tokens_out = tokens_out
        return text

    def _parse_output(self, stdout: str) -> str:
        """Parse the CLI's ``--output-format json`` envelope.

        Falls back to treating the whole stdout as plain text if it isn't
        valid JSON (e.g. an operator-supplied wrapper changes the output
        format, or a future CLI version changes its envelope shape).
        """
        try:
            obj = json.loads(stdout.strip())
        except json.JSONDecodeError:
            return stdout

        if not isinstance(obj, dict):
            return stdout

        if obj.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: {obj.get('result', '')}")

        usage = obj.get("usage")
        if isinstance(usage, dict):
            self._last_tokens_in = int(usage.get("input_tokens", 0) or 0)
            self._last_tokens_out = int(usage.get("output_tokens", 0) or 0)

        result_text = obj.get("result", "")
        return result_text if isinstance(result_text, str) else stdout
