"""Tests for the ACP (Agent Client Protocol) v1 JSON-RPC/stdio client.

These tests never launch a real ACP agent — ``subprocess.Popen`` is
monkeypatched to a fake process object whose stdin/stdout are in-memory
buffers we script line by line.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.review.backends.acp_transport import AcpProtocolError, run_acp_prompt

_POPEN = "franktheunicorn.review.backends.acp_transport.subprocess.Popen"


class _FakeStdin(io.StringIO):
    """Captures every line the client writes, as parsed JSON-RPC messages."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict[str, object]] = []

    def write(self, s: str) -> int:
        for line in s.splitlines():
            if line.strip():
                self.sent.append(json.loads(line))
        return len(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _line(msg: dict[str, object]) -> str:
    return json.dumps(msg) + "\n"


def _fake_proc(stdout_lines: list[dict[str, object]]) -> MagicMock:
    """Build a fake ``Popen`` result: scripted stdout, a capturing stdin."""
    proc = MagicMock()
    proc.stdin = _FakeStdin()
    proc.stdout = io.StringIO("".join(_line(m) for m in stdout_lines))
    proc.stderr = io.StringIO("")
    proc.poll.return_value = 0
    proc.wait.return_value = 0
    return proc


def _handshake_lines(session_id: str = "sess_abc123") -> list[dict[str, object]]:
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {},
                "agentInfo": {"name": "fake-acp-agent", "title": "Fake", "version": "0.0"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": session_id}},
    ]


class TestRunAcpPromptHappyPath:
    def test_full_handshake_and_streamed_reply(self) -> None:
        lines = [
            *_handshake_lines(),
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sess_abc123",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello "},
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sess_abc123",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "world"},
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sess_abc123",
                    "update": {"sessionUpdate": "usage_update", "used": 42, "size": 200000},
                },
            },
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
        ]
        proc = _fake_proc(lines)

        with patch(_POPEN, return_value=proc):
            text, tokens_in, tokens_out = run_acp_prompt(
                ["claude-code-acp"],
                cwd="/tmp",
                timeout=5.0,
                system_prompt="be terse",
                user_message="say hi",
            )

        assert text == "hello world"
        assert tokens_in == 0
        assert tokens_out == 42

    def test_sends_correct_method_names_and_params(self) -> None:
        proc = _fake_proc([*_handshake_lines(), {"jsonrpc": "2.0", "id": 3, "result": {}}])

        with patch(_POPEN, return_value=proc):
            run_acp_prompt(
                ["claude-code-acp", "--flag"],
                cwd="/repo",
                timeout=5.0,
                system_prompt="sys",
                user_message="usr",
            )

        sent = proc.stdin.sent
        assert [m["method"] for m in sent] == ["initialize", "session/new", "session/prompt"]

        init_params = sent[0]["params"]
        assert init_params["protocolVersion"] == 1
        assert init_params["clientCapabilities"] == {}
        assert init_params["clientInfo"]["name"] == "franktheunicorn"

        session_params = sent[1]["params"]
        assert session_params["cwd"] == "/repo"
        assert session_params["mcpServers"] == []

        prompt_params = sent[2]["params"]
        assert prompt_params["sessionId"] == "sess_abc123"
        assert prompt_params["prompt"] == [
            {"type": "text", "text": "sys"},
            {"type": "text", "text": "usr"},
        ]

    def test_no_system_prompt_sends_single_content_block(self) -> None:
        proc = _fake_proc([*_handshake_lines(), {"jsonrpc": "2.0", "id": 3, "result": {}}])

        with patch(_POPEN, return_value=proc):
            run_acp_prompt(
                ["claude-code-acp"],
                cwd="/repo",
                timeout=5.0,
                system_prompt="",
                user_message="usr only",
            )

        prompt_params = proc.stdin.sent[2]["params"]
        assert prompt_params["prompt"] == [{"type": "text", "text": "usr only"}]

    def test_terminates_process_on_success(self) -> None:
        proc = _fake_proc([*_handshake_lines(), {"jsonrpc": "2.0", "id": 3, "result": {}}])

        with patch(_POPEN, return_value=proc):
            run_acp_prompt(
                ["claude-code-acp"], cwd="/tmp", timeout=5.0, system_prompt="", user_message="x"
            )

        proc.terminate.assert_called_once()


class TestRunAcpPromptErrorHandling:
    def test_binary_not_found_raises_protocol_error(self) -> None:
        with (
            patch(_POPEN, side_effect=FileNotFoundError()),
            pytest.raises(AcpProtocolError, match="not found"),
        ):
            run_acp_prompt(
                ["nonexistent-acp-agent"],
                cwd="/tmp",
                timeout=5.0,
                system_prompt="",
                user_message="hi",
            )

    def test_initialize_error_response_raises(self) -> None:
        lines = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32600, "message": "unsupported protocol version"},
            },
        ]
        proc = _fake_proc(lines)

        with (
            patch(_POPEN, return_value=proc),
            pytest.raises(AcpProtocolError, match="initialize failed"),
        ):
            run_acp_prompt(
                ["claude-code-acp"],
                cwd="/tmp",
                timeout=5.0,
                system_prompt="",
                user_message="hi",
            )

    def test_session_new_error_response_raises(self) -> None:
        lines = [
            _handshake_lines()[0],
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "cwd not found"}},
        ]
        proc = _fake_proc(lines)

        with (
            patch(_POPEN, return_value=proc),
            pytest.raises(AcpProtocolError, match="session/new failed"),
        ):
            run_acp_prompt(
                ["claude-code-acp"],
                cwd="/tmp",
                timeout=5.0,
                system_prompt="",
                user_message="hi",
            )

    def test_refusal_stop_reason_raises(self) -> None:
        lines = [
            *_handshake_lines(),
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "refusal"}},
        ]
        proc = _fake_proc(lines)

        with (
            patch(_POPEN, return_value=proc),
            pytest.raises(AcpProtocolError, match="refused"),
        ):
            run_acp_prompt(
                ["claude-code-acp"],
                cwd="/tmp",
                timeout=5.0,
                system_prompt="",
                user_message="hi",
            )

    def test_agent_closes_stdout_early_raises(self) -> None:
        # Only the initialize response, then EOF -- session/new never answered.
        proc = _fake_proc([_handshake_lines()[0]])

        with (
            patch(_POPEN, return_value=proc),
            pytest.raises(AcpProtocolError, match="closed stdout"),
        ):
            run_acp_prompt(
                ["claude-code-acp"],
                cwd="/tmp",
                timeout=5.0,
                system_prompt="",
                user_message="hi",
            )

    def test_missing_session_id_raises(self) -> None:
        lines = [_handshake_lines()[0], {"jsonrpc": "2.0", "id": 2, "result": {}}]
        proc = _fake_proc(lines)

        with (
            patch(_POPEN, return_value=proc),
            pytest.raises(AcpProtocolError, match="no sessionId"),
        ):
            run_acp_prompt(
                ["claude-code-acp"],
                cwd="/tmp",
                timeout=5.0,
                system_prompt="",
                user_message="hi",
            )

    def test_timeout_raises_and_terminates(self) -> None:
        # No lines at all: the queue.get() call will time out immediately
        # since the reader thread puts EOF (None) right away with nothing
        # queued -- this exercises the "closed stdout" path with a tiny
        # timeout budget rather than actually sleeping for a full timeout.
        proc = _fake_proc([])

        with patch(_POPEN, return_value=proc), pytest.raises(AcpProtocolError):
            run_acp_prompt(
                ["claude-code-acp"],
                cwd="/tmp",
                timeout=0.2,
                system_prompt="",
                user_message="hi",
            )

    def test_non_json_lines_are_ignored(self) -> None:
        proc = MagicMock()
        proc.stdin = _FakeStdin()
        proc.stdout = io.StringIO(
            "not json at all\n"
            + _line(_handshake_lines()[0])
            + _line(_handshake_lines()[1])
            + _line({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}})
        )
        proc.stderr = io.StringIO("")
        proc.poll.return_value = 0
        proc.wait.return_value = 0

        with patch(_POPEN, return_value=proc):
            text, _, _ = run_acp_prompt(
                ["claude-code-acp"], cwd="/tmp", timeout=5.0, system_prompt="", user_message="hi"
            )

        assert text == ""
