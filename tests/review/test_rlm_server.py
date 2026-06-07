"""Tests for the broker Unix-socket server + client round trip."""

from __future__ import annotations

from pathlib import Path

from franktheunicorn.config.models import LLMBackendConfig
from franktheunicorn.review.rlm.broker import ModelBroker
from franktheunicorn.review.rlm.protocol import BrokerClient, decode, encode
from franktheunicorn.review.rlm.server import BrokerServer


def test_protocol_encode_decode_roundtrip() -> None:
    assert decode(encode({"op": "models"})) == {"op": "models"}
    assert decode("") == {}


def test_server_client_full_session(tmp_path: Path) -> None:
    broker = ModelBroker(
        {"stub": LLMBackendConfig(provider="stub")},
        max_calls=10,
        default_model="stub",
    )
    sock_path = str(tmp_path / "broker.sock")
    with BrokerServer(broker, sock_path):
        client = BrokerClient(sock_path)
        assert client.models() == ["stub"]
        text = client.llm("review this", model="stub")
        assert "stub completion" in text
        assert client.emit({"file_path": "a.py", "body": "issue", "line": 3})
        assert client.log("progress")

    # Findings/logs were captured host-side.
    assert broker.collected_findings[0]["file_path"] == "a.py"
    assert broker.collected_logs == ["progress"]


def test_server_handles_unknown_op(tmp_path: Path) -> None:
    broker = ModelBroker({"stub": LLMBackendConfig(provider="stub")}, max_calls=5)
    sock_path = str(tmp_path / "b.sock")
    with BrokerServer(broker, sock_path) as server:
        assert server.socket_path == sock_path
        client = BrokerClient(sock_path)
        resp = client._request({"op": "nope"})
    assert resp["ok"] is False
