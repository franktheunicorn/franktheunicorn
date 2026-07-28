"""Fake ACP agent for integration-testing ``acp_transport.acp_complete()``.

Speaks the real protocol over stdio: reads newline-delimited JSON-RPC
messages from stdin, writes responses/notifications to stdout. Spawned by
``test_acp_transport.py`` via ``sys.executable <this file> [mode]`` -- no
mocking of ``subprocess.Popen`` involved, this is a genuine child process
talking real JSON-RPC over a real pipe.

Modes (argv[1]):
  (none)  -- normal flow. Emits a stray non-JSON line before the handshake
             (must be ignored by the client), completes initialize and
             session/new, streams the reply as two ``agent_message_chunk``
             updates, sends a client-bound request the client doesn't
             implement (expects a JSON-RPC error back), then answers
             ``session/prompt`` with ``stopReason: "end_turn"`` and a usage
             block.
  "hang"  -- completes the handshake but never answers ``session/prompt``,
             to exercise the client's timeout path.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any


def _send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _read() -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit("fake_acp_agent: stdin closed unexpectedly")
    return dict(json.loads(line))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    # A stray non-JSON line before the handshake even starts -- the real
    # client must skip this instead of choking on it.
    sys.stdout.write("not json, ignore me\n")
    sys.stdout.flush()

    init_req = _read()
    assert init_req["method"] == "initialize"
    _send(
        {
            "jsonrpc": "2.0",
            "id": init_req["id"],
            "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []},
        }
    )

    session_req = _read()
    assert session_req["method"] == "session/new"
    session_id = "sess-fake-1"
    _send({"jsonrpc": "2.0", "id": session_req["id"], "result": {"sessionId": session_id}})

    prompt_req = _read()
    assert prompt_req["method"] == "session/prompt"

    if mode == "hang":
        # Never respond -- the client should time out and kill us.
        time.sleep(60)
        return

    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Hello "},
                },
            },
        }
    )
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "world!"},
                },
            },
        }
    )

    # Ask the client to do something it doesn't support -- it should decline
    # with a JSON-RPC error rather than hanging on us.
    _send({"jsonrpc": "2.0", "id": "fake-permission-1", "method": "fs/readTextFile", "params": {}})
    decline = _read()
    assert decline.get("id") == "fake-permission-1"
    assert "error" in decline

    _send(
        {
            "jsonrpc": "2.0",
            "id": prompt_req["id"],
            "result": {
                "stopReason": "end_turn",
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        }
    )


if __name__ == "__main__":
    main()
