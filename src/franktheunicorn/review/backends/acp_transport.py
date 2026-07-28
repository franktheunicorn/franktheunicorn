"""Agent Client Protocol (ACP) client: JSON-RPC 2.0 over the agent's stdio.

ACP (https://agentclientprotocol.com/) is a JSON-RPC 2.0 protocol for
editor/client <-> coding-agent communication, originally developed for Zed
and implemented by adapters like ``@zed-industries/claude-code-acp`` (which
wraps the Claude Agent SDK). It is unrelated to the Anthropic Messages API --
it's a separate, community-maintained spec.

Transport: newline-delimited JSON-RPC 2.0 messages, UTF-8, one JSON object
per line, over the subprocess's stdin (client -> agent) and stdout
(agent -> client). stderr is free-form logging we don't parse except to
surface a tail of it on error.

Handshake for a single prompt turn:

1. ``initialize`` (protocolVersion/clientCapabilities/clientInfo) -> agent
   returns protocolVersion/agentCapabilities/authMethods.
2. ``session/new`` (cwd/mcpServers) -> agent returns ``sessionId``.
3. ``session/prompt`` (sessionId/prompt: ContentBlock[]) -- this request
   stays *pending* for the whole turn. The actual reply text does not come
   back in this response; it streams in as ``session/update`` notifications
   (``sessionUpdate: "agent_message_chunk"``, no ``id``) while the request
   is in flight. The agent finally answers ``session/prompt`` itself with
   ``result.stopReason`` once done.

We advertise no filesystem/terminal capabilities, so a well-behaved agent
has nothing to ask us for. If the agent still sends us a client-bound
request (permission, fs, terminal, ...) we don't implement, we decline it
with a generic JSON-RPC "method not found" error rather than hanging
forever waiting on us -- the prompt turn continues normally either way.

This client is single-shot: like the CLI subprocess transport it replaces,
it launches a fresh agent process, does one full handshake, runs exactly one
prompt turn, and tears the process down -- no persistent session is kept
across calls.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = 1
_CLIENT_INFO: dict[str, str] = {
    "name": "franktheunicorn",
    "title": "franktheunicorn",
    "version": "1",
}
# No fs/terminal support advertised -- we're a pure text-completion client.
_CLIENT_CAPABILITIES: dict[str, object] = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}

# JSON-RPC "method not found" -- used to decline any client-bound request we
# don't implement, so a misbehaving agent doesn't hang forever on us.
_METHOD_NOT_FOUND = -32601


class AcpProtocolError(RuntimeError):
    """Raised on any ACP handshake/transport failure: bad JSON-RPC error
    response, a timeout, the agent closing stdout early, or a refusal."""


def _send(stdin: object, msg: dict[str, object]) -> None:
    line = json.dumps(msg) + "\n"
    stdin.write(line)  # type: ignore[attr-defined]
    stdin.flush()  # type: ignore[attr-defined]


def _pump_stdout(stream: object, out_queue: queue.Queue[str | None]) -> None:
    """Read newline-delimited messages from ``stream`` onto ``out_queue``.

    Runs in a background thread so the main thread can enforce a wall-clock
    deadline with ``queue.get(timeout=...)`` instead of blocking on a raw
    ``readline()`` with no way to time out. Puts ``None`` once the stream is
    exhausted (agent exited / closed stdout).
    """
    try:
        for line in stream:  # type: ignore[attr-defined]
            stripped = line.strip()
            if stripped:
                out_queue.put(stripped)
    except (ValueError, OSError):
        # Stream closed out from under us (process killed mid-read).
        pass
    finally:
        out_queue.put(None)


def acp_complete(
    command: list[str],
    text: str,
    *,
    cwd: str,
    timeout: int,
) -> tuple[str, dict[str, int]]:
    """Run one ACP prompt turn end-to-end; return (assistant_text, usage_tokens).

    Launches ``command`` as a subprocess, performs the
    initialize -> session/new -> session/prompt handshake, drains
    ``session/update`` notifications to reassemble the streamed reply, and
    returns once the agent answers the ``session/prompt`` request.

    Token usage is best-effort: if the agent's final ``session/prompt``
    response includes a ``result.usage`` object, its integer fields are
    returned as-is (e.g. ``{"input_tokens": ..., "output_tokens": ...}``);
    if the agent never reports usage, an empty dict is returned.

    Always terminates the subprocess before returning, including on error.
    Raises :class:`AcpProtocolError` (a ``RuntimeError`` subclass) on any
    handshake failure, JSON-RPC error response, refusal, or timeout.
    """
    # Strip CLAUDECODE from the child env: ACP agents that wrap Claude Code
    # (e.g. @zed-industries/claude-code-acp) refuse to launch a nested session
    # when this is set, so a review worker running inside a Claude Code session
    # could not otherwise spawn the agent. The agent itself advises unsetting it.
    child_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise AcpProtocolError(f"ACP agent binary not found: {command[0]!r}") from exc

    out_queue: queue.Queue[str | None] = queue.Queue()
    assert proc.stdout is not None
    assert proc.stdin is not None
    stdin = proc.stdin
    reader = threading.Thread(target=_pump_stdout, args=(proc.stdout, out_queue), daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    accumulated: list[str] = []

    def _handle_update(update: object) -> None:
        if not isinstance(update, dict):
            return
        if update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            chunk = content.get("text")
            if isinstance(chunk, str):
                accumulated.append(chunk)

    def _recv_until(match_id: int) -> dict[str, object]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcpProtocolError(f"ACP agent timed out after {timeout}s")
            try:
                line = out_queue.get(timeout=remaining)
            except queue.Empty:
                raise AcpProtocolError(f"ACP agent timed out after {timeout}s") from None
            if line is None:
                stderr_tail = ""
                if proc.stderr is not None:
                    with contextlib.suppress(ValueError, OSError):
                        stderr_tail = proc.stderr.read()[-2000:]
                raise AcpProtocolError(
                    f"ACP agent closed stdout before responding (exit={proc.poll()}): "
                    f"{stderr_tail or '(no stderr)'}"
                )
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Ignoring malformed line from ACP agent stdout: %s", line)
                continue
            if not isinstance(msg, dict):
                continue

            if msg.get("id") == match_id and ("result" in msg or "error" in msg):
                return msg

            params = msg.get("params")
            if msg.get("method") == "session/update" and isinstance(params, dict):
                _handle_update(params.get("update"))
            elif "method" in msg and "id" in msg:
                # A request from the agent to us (permission, fs, terminal,
                # ...) we don't implement -- we advertise no capabilities for
                # any of these, so a well-behaved agent shouldn't ask, but
                # decline with a proper JSON-RPC error rather than hanging.
                _send(
                    stdin,
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {
                            "code": _METHOD_NOT_FOUND,
                            "message": (
                                f"{msg.get('method')!r} not supported by the "
                                "franktheunicorn ACP client"
                            ),
                        },
                    },
                )

    try:
        _send(
            stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "clientCapabilities": _CLIENT_CAPABILITIES,
                    "clientInfo": _CLIENT_INFO,
                },
            },
        )
        init_result = _recv_until(1)
        if "error" in init_result:
            raise AcpProtocolError(f"ACP initialize failed: {init_result['error']}")

        _send(
            stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"cwd": cwd, "mcpServers": []},
            },
        )
        session_result = _recv_until(2)
        if "error" in session_result:
            raise AcpProtocolError(f"ACP session/new failed: {session_result['error']}")
        session_obj = session_result.get("result")
        session_id = session_obj.get("sessionId") if isinstance(session_obj, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise AcpProtocolError(f"ACP session/new returned no sessionId: {session_result}")

        _send(
            stdin,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            },
        )
        prompt_result = _recv_until(3)
        if "error" in prompt_result:
            raise AcpProtocolError(f"ACP session/prompt failed: {prompt_result['error']}")

        usage: dict[str, int] = {}
        result_obj = prompt_result.get("result")
        if isinstance(result_obj, dict):
            if result_obj.get("stopReason") == "refusal":
                raise AcpProtocolError("ACP agent refused the prompt")
            raw_usage = result_obj.get("usage")
            if isinstance(raw_usage, dict):
                usage = {k: v for k, v in raw_usage.items() if isinstance(v, int)}

        return "".join(accumulated), usage
    finally:
        with contextlib.suppress(ValueError, OSError):
            stdin.close()
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(OSError):
                proc.kill()


__all__ = ["AcpProtocolError", "acp_complete"]
