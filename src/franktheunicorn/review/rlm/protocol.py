"""Newline-delimited JSON protocol between the sandboxed notebook and the host.

The notebook runs inside a ``--network=none`` container, so it cannot reach
any model API directly. Instead it speaks this tiny request/response protocol
to a host-side broker over a bind-mounted Unix domain socket. One request per
connection keeps both ends trivial and robust.

Operations (request ``op``):
- ``models``  → ``{"ok": true, "models": [...]}``
- ``llm``     → ``{"ok": true, "text": "..."}``  (fields: ``model``, ``prompt``, ``system``)
- ``emit``    → ``{"ok": true}``                 (field: ``finding``)
- ``log``     → ``{"ok": true}``                 (field: ``message``)
"""

from __future__ import annotations

import json
import socket
from typing import Any


def encode(obj: dict[str, Any]) -> bytes:
    """Encode a message as a single newline-terminated JSON line."""
    return (json.dumps(obj) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    """Decode one JSON line into a dict (empty dict on blank input)."""
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        return {}
    result: dict[str, Any] = json.loads(text)
    return result


class BrokerClient:
    """Stdlib-only client for the broker socket.

    Mirrors the client embedded in the notebook (see ``notebook.CLIENT_SOURCE``);
    kept here as a real object so the host side and tests can drive the broker
    without spinning up a container.
    """

    def __init__(self, socket_path: str, *, timeout: float = 120.0) -> None:
        self._path = socket_path
        self._timeout = timeout

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self._timeout)
            sock.connect(self._path)
            sock.sendall(encode(payload))
            with sock.makefile("rb") as reader:
                line = reader.readline()
        return decode(line) if line else {"ok": False, "error": "no response"}

    def models(self) -> list[str]:
        resp = self._request({"op": "models"})
        models = resp.get("models", [])
        return list(models) if isinstance(models, list) else []

    def llm(self, prompt: str, model: str | None = None, system: str = "") -> str:
        resp = self._request({"op": "llm", "model": model, "prompt": prompt, "system": system})
        return str(resp.get("text", ""))

    def emit(self, finding: dict[str, Any]) -> bool:
        return bool(self._request({"op": "emit", "finding": finding}).get("ok"))

    def log(self, message: str) -> bool:
        return bool(self._request({"op": "log", "message": message}).get("ok"))
