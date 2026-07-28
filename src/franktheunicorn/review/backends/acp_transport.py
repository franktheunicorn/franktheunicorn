"""Minimal Agent Client Protocol (ACP) v1 client: JSON-RPC over stdio.

ACP (https://agentclientprotocol.com/) is a JSON-RPC 2.0 protocol for
editor/client <-> coding-agent communication, originally developed for Zed
and implemented by adapters like ``claude-code-acp`` (which wraps the Claude
Agent SDK). It is unrelated to the Anthropic Messages API — it's a separate,
community-maintained spec. Real-world agents (as of this writing) implement
protocol **v1**, not the newer v2 draft, so this module targets v1:

* Transport: newline-delimited JSON-RPC 2.0 messages, UTF-8, no embedded
  newlines, over the subprocess's stdin (client -> agent) and stdout
  (agent -> client). stderr is free-form UTF-8 logging we don't parse.
* Handshake: ``initialize`` (protocolVersion/clientCapabilities/clientInfo)
  -> ``session/new`` (cwd/mcpServers) -> ``session/prompt``
  (sessionId/prompt: ContentBlock[]).
* Unlike v2, a v1 agent keeps the ``session/prompt`` request pending for the
  whole turn and answers it with ``{"result": {"stopReason": ...}}`` once
  done; the actual reply text streams in as ``session/update`` notifications
  (``sessionUpdate: "agent_message_chunk"``) while that request is pending.

This client is intentionally single-shot: like the CLI subprocess transport
it replaces, it launches a fresh agent process, does one full handshake, runs
exactly one prompt turn, and tears the process down — no persistent session
is kept across calls. We advertise empty ``clientCapabilities`` (no fs,
terminal, or elicitation support) so a well-behaved agent has nothing to ask
us to do; any client-bound request we still receive is answered with a
generic JSON-RPC error so the agent doesn't hang forever waiting on us.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = 1
_CLIENT_NAME = "franktheunicorn"
_CLIENT_TITLE = "franktheunicorn"
_CLIENT_VERSION = "0.1"

# JSON-RPC "method not found" — used to bounce any client-bound request we
# don't implement, so a misbehaving agent doesn't hang forever on us.
_METHOD_NOT_FOUND = -32601


class AcpProtocolError(RuntimeError):
    """Raised on any ACP handshake/transport failure: bad JSON, a JSON-RPC
    error response, a timeout, or the agent closing stdout early."""


def _send(proc: subprocess.Popen[str], msg: dict[str, object]) -> None:
    line = json.dumps(msg)
    if "\n" in line:
        # json.dumps never emits a literal newline outside of an escaped
        # string, so this should be unreachable — but the spec forbids
        # embedded newlines in a stdio frame, so guard defensively rather
        # than silently corrupting the stream.
        raise AcpProtocolError("refusing to send a JSON-RPC message containing a raw newline")
    assert proc.stdin is not None
    proc.stdin.write(line + "\n")
    proc.stdin.flush()


def _pump_stdout(stream: object, out_queue: queue.Queue[str | None]) -> None:
    """Read newline-delimited messages from ``stream`` onto ``out_queue``.

    Runs in a background thread so the main thread can enforce a wall-clock
    deadline with ``queue.get(timeout=...)`` instead of blocking on a raw
    ``readline()`` with no way to time out. Puts ``None`` once the stream
    is exhausted (agent exited / closed stdout).
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


def run_acp_prompt(
    agent_argv: list[str],
    *,
    cwd: str,
    timeout: float,
    system_prompt: str,
    user_message: str,
) -> tuple[str, int, int]:
    """Run one ACP v1 prompt turn end-to-end; return (text, tokens_in, tokens_out).

    Launches ``agent_argv`` as a subprocess, performs the
    initialize -> session/new -> session/prompt handshake, drains
    ``session/update`` notifications to reassemble the streamed reply, and
    returns once the agent answers the ``session/prompt`` request. Token
    counts are best-effort: ACP's ``usage_update`` reports context-window
    usage (``used``/``size``), not a clean input/output split, so
    ``tokens_in`` is always ``0`` here and ``tokens_out`` holds the last
    ``used`` value seen (or ``0`` if the agent never sends one).

    Always terminates the subprocess before returning, including on error.
    Raises :class:`AcpProtocolError` on any handshake failure, JSON-RPC
    error response, refusal, or timeout.
    """
    try:
        proc = subprocess.Popen(
            agent_argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise AcpProtocolError(f"ACP agent binary not found: {agent_argv[0]!r}") from exc

    out_queue: queue.Queue[str | None] = queue.Queue()
    assert proc.stdout is not None
    reader = threading.Thread(target=_pump_stdout, args=(proc.stdout, out_queue), daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    accumulated_text: list[str] = []
    tokens_out = 0
    next_id = 0

    def _new_id() -> int:
        nonlocal next_id
        next_id += 1
        return next_id

    def _handle_update(update: dict[str, object]) -> None:
        nonlocal tokens_out
        kind = update.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_message"):
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str):
                accumulated_text.append(text)
        elif kind == "usage_update":
            used = update.get("used")
            if isinstance(used, int):
                tokens_out = used

    def _recv_until(match_id: int) -> dict[str, object]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcpProtocolError(f"ACP agent timed out after {timeout:g}s")
            try:
                line = out_queue.get(timeout=remaining)
            except queue.Empty:
                raise AcpProtocolError(f"ACP agent timed out after {timeout:g}s") from None
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
                logger.debug("Ignoring non-JSON line from ACP agent stdout: %s", line)
                continue
            if not isinstance(msg, dict):
                continue

            if msg.get("id") == match_id and ("result" in msg or "error" in msg):
                return msg

            if msg.get("method") == "session/update" and "params" in msg:
                params = msg["params"]
                update = params.get("update") if isinstance(params, dict) else None
                if isinstance(update, dict):
                    _handle_update(update)
            elif "method" in msg and "id" in msg:
                # A request from the agent to us (permission, fs, elicitation,
                # ...) that we don't implement -- we advertise no capabilities
                # for any of these, so a well-behaved agent shouldn't ask, but
                # answer with a proper JSON-RPC error rather than hanging.
                _send(
                    proc,
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
        init_id = _new_id()
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": init_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "clientCapabilities": {},
                    "clientInfo": {
                        "name": _CLIENT_NAME,
                        "title": _CLIENT_TITLE,
                        "version": _CLIENT_VERSION,
                    },
                },
            },
        )
        init_result = _recv_until(init_id)
        if "error" in init_result:
            raise AcpProtocolError(f"ACP initialize failed: {init_result['error']}")

        session_req_id = _new_id()
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": session_req_id,
                "method": "session/new",
                "params": {"cwd": cwd, "mcpServers": []},
            },
        )
        session_result = _recv_until(session_req_id)
        if "error" in session_result:
            raise AcpProtocolError(f"ACP session/new failed: {session_result['error']}")
        result_obj = session_result.get("result")
        session_id = result_obj.get("sessionId") if isinstance(result_obj, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise AcpProtocolError(f"ACP session/new returned no sessionId: {session_result}")

        prompt_blocks: list[dict[str, str]] = []
        if system_prompt:
            prompt_blocks.append({"type": "text", "text": system_prompt})
        prompt_blocks.append({"type": "text", "text": user_message})

        prompt_req_id = _new_id()
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": prompt_req_id,
                "method": "session/prompt",
                "params": {"sessionId": session_id, "prompt": prompt_blocks},
            },
        )
        prompt_result = _recv_until(prompt_req_id)
        if "error" in prompt_result:
            raise AcpProtocolError(f"ACP session/prompt failed: {prompt_result['error']}")

        result_obj = prompt_result.get("result")
        stop_reason = result_obj.get("stopReason") if isinstance(result_obj, dict) else None
        if stop_reason == "refusal":
            raise AcpProtocolError("ACP agent refused the prompt")

        return "".join(accumulated_text), 0, tokens_out
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (ValueError, OSError):
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(OSError):
                proc.kill()


__all__ = ["AcpProtocolError", "run_acp_prompt"]
