"""Tests for the triage backend circuit breaker.

Two halves: the boot smoke test that 86s backends that fail their probe, and
the runtime circuit breaker that marks a backend down after a hard failure
(403 / connection refused) so a dead backend is tried once, not 86 times across
a backlog. When no triage backend is alive the worker leaves triage commands
pending instead of burning through them.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, SecurityTriageConfig
from franktheunicorn.review.backends.base import BaseLLMBackend


def _operator_config(
    *, llm_backends: list[LLMBackendConfig] | None = None, override: LLMBackendConfig | None = None
) -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        llm_backends=llm_backends or [],
        security_triage=SecurityTriageConfig(enabled=True, llm_backend=override),
    )


@pytest.fixture(autouse=True)
def _reset_health() -> None:
    from franktheunicorn.security.triage import _triage_backend_health

    _triage_backend_health.reset()
    yield
    _triage_backend_health.reset()


class TestBackendHealth:
    def test_a_backend_is_alive_until_marked_down(self) -> None:
        from franktheunicorn.security.triage import _backend_key, _triage_backend_health

        cfg = LLMBackendConfig(provider="stub")
        key = _backend_key(cfg)
        assert _triage_backend_health.is_down(key) is False
        _triage_backend_health.mark_down(key)
        assert _triage_backend_health.is_down(key) is True

    def test_mark_alive_clears_a_down_mark(self) -> None:
        from franktheunicorn.security.triage import _backend_key, _triage_backend_health

        key = _backend_key(LLMBackendConfig(provider="stub"))
        _triage_backend_health.mark_down(key)
        _triage_backend_health.mark_alive(key)
        assert _triage_backend_health.is_down(key) is False

    def test_any_alive_reflects_the_set(self) -> None:
        from franktheunicorn.security.triage import _backend_key, _triage_backend_health

        a = _backend_key(LLMBackendConfig(provider="stub"))
        b = _backend_key(LLMBackendConfig(provider="ollama", model="qwen"))
        _triage_backend_health.mark_down(a)
        configs = [
            LLMBackendConfig(provider="stub"),
            LLMBackendConfig(provider="ollama", model="qwen"),
        ]
        assert _triage_backend_health.any_alive(configs) is True
        _triage_backend_health.mark_down(b)
        assert _triage_backend_health.any_alive(configs) is False

    def test_a_down_mark_expires_after_the_cooldown(self) -> None:
        from franktheunicorn.security.triage import (
            _BACKEND_DOWN_COOLDOWN_S,
            _backend_key,
            _BackendHealth,
        )

        health = _BackendHealth()
        key = _backend_key(LLMBackendConfig(provider="stub"))
        # Force a down mark with a zero-length cooldown so it's already expired.
        with patch.object(time, "monotonic", side_effect=[0.0, 0.0]):
            health.mark_down(key)
        # monotonic now past the deadline → is_down clears and returns False.
        with patch.object(time, "monotonic", return_value=_BACKEND_DOWN_COOLDOWN_S + 1.0):
            assert health.is_down(key) is False
        # And the clearing sticks — a second ask without re-marking stays alive.
        with patch.object(time, "monotonic", return_value=_BACKEND_DOWN_COOLDOWN_S + 2.0):
            assert health.is_down(key) is False


@pytest.mark.django_db
class TestSeedTriageBackendHealth:
    def test_a_dead_ollama_is_marked_down_and_skipped_by_triage(self) -> None:
        from franktheunicorn.security.triage import (
            _get_triage_backends,
            seed_triage_backend_health,
        )

        cfg = _operator_config(
            llm_backends=[LLMBackendConfig(provider="ollama", model="qwen2.5-coder:14b")]
        )
        with patch(
            "franktheunicorn.review.backends.preflight._probe_ollama",
            return_value=MagicMock(ok=False, reason="ConnectionError: refused", probed=True),
        ):
            total, disabled = seed_triage_backend_health(cfg)

        assert disabled == 1
        assert total == 1
        # _get_triage_backends skips the down backend → triage has nothing to call.
        assert _get_triage_backends(cfg) == []

    def test_a_live_backend_stays_alive(self) -> None:
        from franktheunicorn.security.triage import (
            _get_triage_backends,
            seed_triage_backend_health,
        )

        cfg = _operator_config(llm_backends=[LLMBackendConfig(provider="stub")])
        total, disabled = seed_triage_backend_health(cfg)
        assert (total, disabled) == (1, 0)
        assert len(_get_triage_backends(cfg)) == 1

    def test_the_override_is_probed_too(self) -> None:
        from franktheunicorn.security.triage import seed_triage_backend_health

        cfg = _operator_config(
            llm_backends=[LLMBackendConfig(provider="stub")],
            override=LLMBackendConfig(provider="ollama", model="qwen"),
        )
        with patch(
            "franktheunicorn.review.backends.preflight._probe_ollama",
            return_value=MagicMock(ok=False, reason="ConnectionError: refused", probed=True),
        ):
            _, disabled = seed_triage_backend_health(cfg)
        # Override dead + stub unchecked → only the override is disabled.
        assert disabled == 1


class _StatusError(Exception):
    """A real exception carrying an HTTP status code, for tests.

    MagicMock breaks looks_offline's ``__cause__``/``__context__`` walk (dunder
    attrs aren't auto-created), so use a real exception instead.
    """

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class TestIsHardFailure:
    def test_a_connection_error_is_hard(self) -> None:
        from franktheunicorn.review.backends.base import is_hard_failure

        assert is_hard_failure(ConnectionError("connection refused")) is True

    def test_a_403_is_hard(self) -> None:
        from franktheunicorn.review.backends.base import is_hard_failure

        assert is_hard_failure(_StatusError(403)) is True

    def test_a_401_is_hard(self) -> None:
        from franktheunicorn.review.backends.base import is_hard_failure

        assert is_hard_failure(_StatusError(401)) is True

    def test_a_429_is_not_hard(self) -> None:
        from franktheunicorn.review.backends.base import is_hard_failure

        assert is_hard_failure(_StatusError(429)) is False

    def test_a_500_is_not_hard(self) -> None:
        from franktheunicorn.review.backends.base import is_hard_failure

        assert is_hard_failure(_StatusError(500)) is False


class _RaisingBackend(BaseLLMBackend):
    """A backend standing in for one whose call raises — only needs the config
    key that ``_mark_backend_down_if_hard`` reads off it."""

    _sdk_module = ""
    _default_key_env = ""
    _default_model = "test"

    def __init__(self, exc: BaseException) -> None:
        super().__init__(LLMBackendConfig(provider="stub"))
        self._exc = exc

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        raise self._exc


class TestRuntimeCircuitBreaker:
    """A hard failure on a real triage call marks the backend down so the next
    report skips it; a transient failure (429) does not."""

    def test_a_403_marks_the_backend_down(self) -> None:
        from franktheunicorn.security.triage import (
            _backend_key,
            _mark_backend_down_if_hard,
            _triage_backend_health,
        )

        backend = _RaisingBackend(_StatusError(403))
        _mark_backend_down_if_hard(backend, backend._exc)
        assert _triage_backend_health.is_down(_backend_key(backend._config)) is True

    def test_a_connection_error_marks_the_backend_down(self) -> None:
        from franktheunicorn.security.triage import (
            _backend_key,
            _mark_backend_down_if_hard,
            _triage_backend_health,
        )

        backend = _RaisingBackend(ConnectionError("refused"))
        _mark_backend_down_if_hard(backend, backend._exc)
        assert _triage_backend_health.is_down(_backend_key(backend._config)) is True

    def test_a_429_does_not_mark_the_backend_down(self) -> None:
        from franktheunicorn.security.triage import (
            _backend_key,
            _mark_backend_down_if_hard,
            _triage_backend_health,
        )

        backend = _RaisingBackend(_StatusError(429))
        _mark_backend_down_if_hard(backend, backend._exc)
        assert _triage_backend_health.is_down(_backend_key(backend._config)) is False

    def test_a_down_backend_is_skipped_on_the_next_triage(self) -> None:
        from franktheunicorn.security.triage import (
            _get_triage_backends,
            _mark_backend_down_if_hard,
        )

        cfg = _operator_config(llm_backends=[LLMBackendConfig(provider="stub")])
        backend = _RaisingBackend(_StatusError(403))
        _mark_backend_down_if_hard(backend, backend._exc)
        # The stub backend whose key matches the down one is skipped.
        assert _get_triage_backends(cfg) == []


@pytest.mark.django_db
class TestWorkerPause:
    def test_triage_commands_are_left_pending_when_no_backend_alive(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from franktheunicorn.security.triage import _backend_key, _triage_backend_health
        from franktheunicorn.worker.commands import process_pending_commands
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(raw_text="needs triage")
        # Queue a triage command and a non-triage command.
        from franktheunicorn.core.models import WorkerCommand

        WorkerCommand.objects.create(command="run_security_triage", security_report=report)
        WorkerCommand.objects.create(command="map_report_versions", security_report=report)
        # Mark every triage backend down.
        _triage_backend_health.mark_down(_backend_key(LLMBackendConfig(provider="stub")))
        cfg = _operator_config(llm_backends=[LLMBackendConfig(provider="stub")])

        processed = process_pending_commands(cfg, limit=10)

        # The version-map command ran; the triage command was left pending.
        assert WorkerCommand.objects.filter(
            command="run_security_triage", status="pending"
        ).exists()
        assert not WorkerCommand.objects.filter(
            command="map_report_versions", status="pending"
        ).exists()
        assert processed >= 1
