"""Host-side model broker for the recursive notebook.

The broker is the only thing in the RLM that holds API keys and network
access. The sandboxed notebook asks it to (a) list the models available, (b)
run a completion against *any* of them, and (c) record findings. A per-session
call budget caps cost. The broker is transport-agnostic; ``BrokerServer``
(server.py) wraps it in a Unix-socket loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)


class ModelBroker:
    """Routes ``llm`` calls to any configured backend and collects findings."""

    def __init__(
        self,
        model_configs: dict[str, LLMBackendConfig],
        *,
        max_calls: int,
        default_model: str | None = None,
        project_id: int | None = None,
        pr_id: int | None = None,
    ) -> None:
        self._configs = dict(model_configs)
        self._max_calls = max_calls
        self._default = default_model or (next(iter(self._configs), None))
        self._project_id = project_id
        self._pr_id = pr_id
        self._calls = 0
        self._findings: list[dict[str, Any]] = []
        self._logs: list[str] = []

    def available_models(self) -> list[str]:
        return list(self._configs)

    @property
    def calls_used(self) -> int:
        return self._calls

    def call_model(self, model: str | None, prompt: str, system: str = "") -> str:
        """Run a raw completion against ``model`` (or the default), budget-capped."""
        if self._calls >= self._max_calls:
            logger.warning("RLM broker call budget (%d) exhausted.", self._max_calls)
            return "[error: model-call budget exhausted]"

        name = model or self._default
        config = self._configs.get(name) if name is not None else None
        if config is None:
            return f"[error: unknown model {model!r}; available: {self.available_models()}]"

        self._calls += 1
        from franktheunicorn.review.backends import get_backend

        backend = get_backend(config)
        try:
            text = backend.complete(prompt, system=system)
        except Exception:
            logger.debug("RLM broker: model %s failed.", name, exc_info=True)
            return "[error: model call failed]"

        record = getattr(backend, "record_cost", None)
        if callable(record):
            try:
                record(self._project_id, self._pr_id, "rlm-notebook")
            except Exception:
                logger.debug("RLM broker: failed to record cost.", exc_info=True)
        return text

    def record_finding(self, finding: dict[str, Any]) -> None:
        self._findings.append(finding)

    def record_log(self, message: str) -> None:
        self._logs.append(message)

    @property
    def collected_findings(self) -> list[dict[str, Any]]:
        return list(self._findings)

    @property
    def collected_logs(self) -> list[str]:
        return list(self._logs)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one decoded protocol request to a response dict."""
        op = request.get("op")
        if op == "models":
            return {"ok": True, "models": self.available_models()}
        if op == "llm":
            text = self.call_model(
                request.get("model"),
                str(request.get("prompt", "")),
                str(request.get("system", "")),
            )
            return {"ok": True, "text": text}
        if op == "emit":
            finding = request.get("finding")
            if isinstance(finding, dict):
                self.record_finding(finding)
                return {"ok": True}
            return {"ok": False, "error": "emit requires a 'finding' object"}
        if op == "log":
            self.record_log(str(request.get("message", "")))
            return {"ok": True}
        return {"ok": False, "error": f"unknown op {op!r}"}
