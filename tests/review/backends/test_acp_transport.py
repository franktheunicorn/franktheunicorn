"""Tests for the ACP (Agent Client Protocol) JSON-RPC/stdio client.

Two layers of coverage:

* ``TestAcpCompleteIntegration`` spawns a real fake ACP agent (see
  ``fixtures/fake_acp_agent.py``) as a genuine subprocess via
  ``sys.executable`` -- real stdio, real JSON-RPC framing, no mocking of
  ``subprocess.Popen``. This exercises the actual happy path (including a
  stray non-JSON line the client must ignore, and a client-bound request
  the agent sends that we must decline) plus the timeout path.
* ``TestAcpCompleteErrorHandling`` monkeypatches ``subprocess.Popen`` with a
  fake process whose stdin/stdout are in-memory buffers, for fast/precise
  coverage of individual JSON-RPC error responses.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.review.backends.acp_transport import AcpProtocolError, acp_complete

_POPEN = "franktheunicorn.review.backends.acp_transport.subprocess.Popen"
_FAKE_AGENT = str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")


class TestAcpCompleteIntegration:
    """Spawns the real fake_acp_agent.py fixture as a subprocess."""

    def test_full_handshake_streamed_reply_and_unexpected_request_declined(
        self, tmp_path: Path
    ) -> None:
        text, usage = acp_complete(
            [sys.executable, _FAKE_AGENT],
            "say hi",
            cwd=str(tmp_path),
            timeout=10,
        )

        assert text == "Hello world!"
        assert usage == {"input_tokens": 7, "output_tokens": 3}

    def test_timeout_raises_and_kills_hung_agent(self, tmp_path: Path) -> None:
        with pytest.raises(AcpProtocolError, match="timed out"):
            acp_complete(
                [sys.executable, _FAKE_AGENT, "hang"],
                "say hi",
                cwd=str(tmp_path),
                timeout=1,
            )


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
            "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []},
        },
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": session_id}},
    ]


class TestAcpCompleteMockedHappyPath:
    def test_sends_correct_method_names_and_params(self) -> None:
        proc = _fake_proc([*_handshake_lines(), {"jsonrpc": "2.0", "id": 3, "result": {}}])

        with patch(_POPEN, return_value=proc):
            acp_complete(["claude-code-acp", "--flag"], "combined text", cwd="/repo", timeout=5)

        sent = proc.stdin.sent
        assert [m["method"] for m in sent] == ["initialize", "session/new", "session/prompt"]

        init_params = sent[0]["params"]
        assert init_params["protocolVersion"] == 1
        assert init_params["clientCapabilities"] == {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        }
        assert init_params["clientInfo"]["name"] == "franktheunicorn"

        session_params = sent[1]["params"]
        assert session_params["cwd"] == "/repo"
        assert session_params["mcpServers"] == []

        prompt_params = sent[2]["params"]
        assert prompt_params["sessionId"] == "sess_abc123"
        assert prompt_params["prompt"] == [{"type": "text", "text": "combined text"}]

    def test_returns_usage_from_final_response(self) -> None:
        lines = [
            *_handshake_lines(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 12, "output_tokens": 34},
                },
            },
        ]
        proc = _fake_proc(lines)

        with patch(_POPEN, return_value=proc):
            text, usage = acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=5)

        assert text == ""
        assert usage == {"input_tokens": 12, "output_tokens": 34}

    def test_no_usage_in_response_returns_empty_dict(self) -> None:
        proc = _fake_proc([*_handshake_lines(), {"jsonrpc": "2.0", "id": 3, "result": {}}])

        with patch(_POPEN, return_value=proc):
            _, usage = acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=5)

        assert usage == {}

    def test_terminates_process_on_success(self) -> None:
        proc = _fake_proc([*_handshake_lines(), {"jsonrpc": "2.0", "id": 3, "result": {}}])

        with patch(_POPEN, return_value=proc):
            acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=5)

        proc.terminate.assert_called_once()

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
            text, _usage = acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=5)

        assert text == ""


class TestAcpCompleteErrorHandling:
    def test_binary_not_found_raises_protocol_error(self) -> None:
        with (
            patch(_POPEN, side_effect=FileNotFoundError()),
            pytest.raises(AcpProtocolError, match="not found"),
        ):
            acp_complete(["nonexistent-acp-agent"], "hi", cwd="/tmp", timeout=5)

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
            acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=5)

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
            acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=5)

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
            acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=5)

    def test_agent_closes_stdout_early_raises(self) -> None:
        # Only the initialize response, then EOF -- session/new never answered.
        proc = _fake_proc([_handshake_lines()[0]])

        with (
            patch(_POPEN, return_value=proc),
            pytest.raises(AcpProtocolError, match="closed stdout"),
        ):
            acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=5)

    def test_missing_session_id_raises(self) -> None:
        lines = [_handshake_lines()[0], {"jsonrpc": "2.0", "id": 2, "result": {}}]
        proc = _fake_proc(lines)

        with (
            patch(_POPEN, return_value=proc),
            pytest.raises(AcpProtocolError, match="no sessionId"),
        ):
            acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=5)

    def test_mocked_timeout_raises(self) -> None:
        # No lines at all: the reader thread puts EOF (None) right away with
        # nothing queued -- exercises the "closed stdout" timeout path
        # without actually sleeping for a full timeout.
        proc = _fake_proc([])

        with patch(_POPEN, return_value=proc), pytest.raises(AcpProtocolError):
            acp_complete(["claude-code-acp"], "hi", cwd="/tmp", timeout=1)
