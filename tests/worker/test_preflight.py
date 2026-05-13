"""Tests for _openai_chat_preflight and _seed_token_param_fallback in the worker runner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.worker.runner import _openai_chat_preflight


def _make_bad_request(message: str) -> object:
    import openai

    resp = MagicMock()
    resp.status_code = 400
    resp.headers = {}
    resp.text = message
    return openai.BadRequestError(message=message, response=resp, body={"error": message})


def _make_client_mock(side_effects: list[object]) -> MagicMock:
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = side_effects
    return mock_client


@pytest.mark.django_db
class TestOpenAIChatPreflight:
    _CLIENT_KW: dict[str, str] = {"api_key": "fake-key"}
    _MODEL = "cortex-model"
    _BASE_URL = "https://snowhouse.example.com/api/v2/cortex/v1"

    def _run(self, side_effects: list[object]) -> set[int]:
        disabled: set[int] = set()
        mock_client = _make_client_mock(side_effects)
        with patch("openai.OpenAI", return_value=mock_client):
            _openai_chat_preflight(
                None,
                self._CLIENT_KW,
                self._MODEL,
                self._BASE_URL,
                idx=1,
                masked="fa…ey",
                disabled=disabled,
            )
        return disabled

    def test_success_first_attempt_does_not_disable(self) -> None:
        disabled = self._run([None])
        assert disabled == set()

    def test_success_first_attempt_no_fallback_row(self) -> None:
        from franktheunicorn.core.models import LLMBackendFallback

        self._run([None])
        assert not LLMBackendFallback.objects.filter(
            provider="openai", model=self._MODEL, base_url=self._BASE_URL
        ).exists()

    def test_retries_on_max_tokens_deprecation_error(self) -> None:
        err = _make_bad_request("max_tokens is deprecated in favor of max_completion_tokens")
        disabled = self._run([err, None])
        assert disabled == set()

    def test_seeds_fallback_row_after_successful_retry(self) -> None:
        from franktheunicorn.core.models import LLMBackendFallback

        err = _make_bad_request("max_tokens is deprecated in favor of max_completion_tokens")
        self._run([err, None])
        row = LLMBackendFallback.objects.filter(
            provider="openai", model=self._MODEL, base_url=self._BASE_URL
        ).first()
        assert row is not None
        assert row.token_param == "max_completion_tokens"

    def test_disables_on_second_attempt_failure(self) -> None:
        err1 = _make_bad_request("max_tokens is deprecated in favor of max_completion_tokens")
        err2 = _make_bad_request("max_completion_tokens also not supported")
        disabled = self._run([err1, err2])
        assert 1 in disabled

    def test_disables_on_unrelated_400_without_retry(self) -> None:
        import openai

        mock_client = MagicMock()
        call_count = 0

        def _side_effect(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 400
            resp.headers = {}
            resp.text = "model not found"
            raise openai.BadRequestError(
                message="model not found", response=resp, body={"error": "model not found"}
            )

        mock_client.chat.completions.create.side_effect = _side_effect
        disabled: set[int] = set()
        with patch("openai.OpenAI", return_value=mock_client):
            _openai_chat_preflight(
                None,
                self._CLIENT_KW,
                self._MODEL,
                self._BASE_URL,
                idx=1,
                masked="fa…ey",
                disabled=disabled,
            )
        assert 1 in disabled
        assert call_count == 1

    def test_disables_on_non_bad_request_exception(self) -> None:
        disabled = self._run([ConnectionError("timeout")])
        assert 1 in disabled
