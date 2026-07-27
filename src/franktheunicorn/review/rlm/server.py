"""Unix-socket server that exposes a :class:`ModelBroker` to the notebook.

Runs on a host thread for the lifetime of one notebook execution. The socket
file is bind-mounted into the sandboxed container; the notebook connects to it
for every ``llm``/``models``/``emit`` call. Each connection carries exactly one
request and one response, so handling is a simple read-line / dispatch / write.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import TYPE_CHECKING

from franktheunicorn.review.rlm.protocol import decode, encode

if TYPE_CHECKING:
    from franktheunicorn.review.rlm.broker import ModelBroker

logger = logging.getLogger(__name__)

_ACCEPT_TIMEOUT = 0.5  # seconds; lets the loop notice stop() promptly


class BrokerServer:
    """Threaded Unix-socket front end for a ModelBroker."""

    def __init__(self, broker: ModelBroker, socket_path: str) -> None:
        self._broker = broker
        self._socket_path = socket_path
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    @property
    def socket_path(self) -> str:
        return self._socket_path

    def start(self) -> None:
        """Bind, listen, and serve in a background daemon thread."""
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self._socket_path)
        sock.listen(8)
        sock.settimeout(_ACCEPT_TIMEOUT)
        self._sock = sock
        self._running.set()
        self._thread = threading.Thread(target=self._serve, name="rlm-broker", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        assert self._sock is not None
        while self._running.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                # The accept timeout fires every ~0.5s while idle so the loop
                # can notice stop(). On Python 3.10+ socket.timeout is an alias
                # of TimeoutError, so this branch (not the OSError one below)
                # catches it and the server keeps serving.
                continue
            except OSError:
                break
            with conn:
                self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        try:
            with conn.makefile("rb") as reader:
                line = reader.readline()
            request = decode(line) if line else {}
            response = self._broker.handle(request)
        except Exception as exc:
            logger.debug("RLM broker: error handling request.", exc_info=True)
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            conn.sendall(encode(response))
        except OSError:
            logger.debug("RLM broker: client hung up before response.")

    def stop(self) -> None:
        """Stop serving and remove the socket file."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except OSError:
                logger.debug("RLM broker: could not unlink socket %s", self._socket_path)

    def __enter__(self) -> BrokerServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
