"""Tests for the host-side model broker."""

from __future__ import annotations

from unittest.mock import patch

from franktheunicorn.config.models import LLMBackendConfig
from franktheunicorn.review.rlm.broker import ModelBroker


class _FakeBackend:
    def __init__(self, config: LLMBackendConfig) -> None:
        self.config = config
        self.cost_calls: list[tuple[int | None, int | None, str]] = []

    def complete(self, prompt: str, *, system: str = "") -> str:
        return f"{self.config.provider}:{prompt}"

    def record_cost(self, project_id: int | None, pr_id: int | None, action_type: str) -> None:
        self.cost_calls.append((project_id, pr_id, action_type))


def _configs() -> dict[str, LLMBackendConfig]:
    return {
        "claude-x": LLMBackendConfig(provider="claude", model="claude-x"),
        "gpt-x": LLMBackendConfig(provider="openai", model="gpt-x"),
    }


def test_available_models_lists_all() -> None:
    broker = ModelBroker(_configs(), max_calls=10)
    assert set(broker.available_models()) == {"claude-x", "gpt-x"}


def test_call_routes_to_named_model() -> None:
    broker = ModelBroker(_configs(), max_calls=10, project_id=1, pr_id=2)
    with patch(
        "franktheunicorn.review.backends.get_backend", side_effect=lambda c: _FakeBackend(c)
    ):
        assert broker.call_model("gpt-x", "hello") == "openai:hello"
        assert broker.call_model("claude-x", "hi") == "claude:hi"


def test_call_uses_default_when_model_none() -> None:
    broker = ModelBroker(_configs(), max_calls=10, default_model="gpt-x")
    with patch(
        "franktheunicorn.review.backends.get_backend", side_effect=lambda c: _FakeBackend(c)
    ):
        assert broker.call_model(None, "x").startswith("openai:")


def test_unknown_model_is_reported() -> None:
    broker = ModelBroker(_configs(), max_calls=10)
    with patch(
        "franktheunicorn.review.backends.get_backend", side_effect=lambda c: _FakeBackend(c)
    ):
        assert "unknown model" in broker.call_model("nope", "x")


def test_call_budget_enforced() -> None:
    broker = ModelBroker(_configs(), max_calls=1, default_model="gpt-x")
    with patch(
        "franktheunicorn.review.backends.get_backend", side_effect=lambda c: _FakeBackend(c)
    ):
        assert broker.call_model(None, "a").startswith("openai:")
        assert "budget exhausted" in broker.call_model(None, "b")


def test_handle_dispatches_ops() -> None:
    broker = ModelBroker(_configs(), max_calls=10, default_model="gpt-x")
    with patch(
        "franktheunicorn.review.backends.get_backend", side_effect=lambda c: _FakeBackend(c)
    ):
        assert broker.handle({"op": "models"})["models"]
        assert broker.handle({"op": "llm", "prompt": "p"})["text"].startswith("openai:")
    assert broker.handle({"op": "emit", "finding": {"file_path": "a.py", "body": "x"}})["ok"]
    assert broker.collected_findings[0]["file_path"] == "a.py"
    assert broker.handle({"op": "log", "message": "hi"})["ok"]
    assert broker.collected_logs == ["hi"]
    assert broker.handle({"op": "emit", "finding": "bad"})["ok"] is False
    assert broker.handle({"op": "frobnicate"})["ok"] is False


def test_stub_backend_complete_is_deterministic() -> None:
    from franktheunicorn.review.backends.stub_backend import StubBackend

    backend = StubBackend(LLMBackendConfig(provider="stub"))
    out = backend.complete("hello world")
    assert out == backend.complete("hello world")
    assert "stub completion" in out
