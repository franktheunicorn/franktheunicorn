"""Tests for OpenAIBackend DB-persisted fallback state."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig
from franktheunicorn.core.models import LLMBackendFallback
from franktheunicorn.review.backends.openai_backend import OpenAIBackend
from tests.factories import LLMBackendFallbackFactory


def _make_config(model: str = "gpt-4o", base_url: str = "") -> LLMBackendConfig:
    return LLMBackendConfig(provider="openai", model=model, base_url=base_url)


@pytest.mark.django_db
class TestOpenAIBackendLoadsFromDB:
    """init picks up persisted fallback flags when a DB row exists."""

    def test_loads_token_param_from_db(self) -> None:
        LLMBackendFallbackFactory(
            provider="openai",
            model="gpt-4o",
            base_url="",
            token_param="max_tokens",
            supports_json_object=True,
        )
        backend = OpenAIBackend(_make_config())
        assert backend._token_param == "max_tokens"

    def test_loads_supports_json_object_false_from_db(self) -> None:
        LLMBackendFallbackFactory(
            provider="openai",
            model="gpt-4o",
            base_url="",
            token_param="max_completion_tokens",
            supports_json_object=False,
        )
        backend = OpenAIBackend(_make_config())
        assert backend._supports_json_object is False

    def test_no_db_row_uses_optimistic_defaults(self) -> None:
        backend = OpenAIBackend(_make_config(model="gpt-4-turbo"))
        assert backend._token_param == "max_completion_tokens"
        assert backend._supports_json_object is True

    def test_loads_by_base_url(self) -> None:
        LLMBackendFallbackFactory(
            provider="openai",
            model="custom",
            base_url="http://localhost:8000",
            token_param="max_tokens",
            supports_json_object=False,
        )
        backend = OpenAIBackend(_make_config(model="custom", base_url="http://localhost:8000"))
        assert backend._token_param == "max_tokens"
        assert backend._supports_json_object is False

    def test_ignores_row_for_different_base_url(self) -> None:
        LLMBackendFallbackFactory(
            provider="openai",
            model="gpt-4o",
            base_url="http://other:9000",
            token_param="max_tokens",
            supports_json_object=False,
        )
        backend = OpenAIBackend(_make_config(model="gpt-4o", base_url=""))
        # Row is for a different base_url — defaults should apply.
        assert backend._token_param == "max_completion_tokens"
        assert backend._supports_json_object is True


@pytest.mark.django_db
class TestOpenAIBackendPersistsFallback:
    """_call_api writes fallback state to DB when a BadRequestError triggers it."""

    def _bad_request(self, message: str) -> MagicMock:
        import openai

        resp = MagicMock()
        resp.status_code = 400
        resp.headers = {}
        resp.text = message
        return openai.BadRequestError(message=message, response=resp, body={"error": message})

    def test_json_object_fallback_creates_db_row(self) -> None:
        config = _make_config(model="gpt-legacy")
        backend = OpenAIBackend(config)
        assert backend._supports_json_object is True

        bad_req = self._bad_request("response_format json_object not supported")
        good_resp = MagicMock()
        good_resp.choices = [MagicMock()]
        good_resp.choices[0].message.content = "[]"
        good_resp.usage = None

        call_count = 0

        def _fake_create(**kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise bad_req
            return good_resp

        with patch("openai.OpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = _fake_create
            backend._call_api("sys", "user", "fake-key")

        assert backend._supports_json_object is False
        row = LLMBackendFallback.objects.get(provider="openai", model="gpt-legacy", base_url="")
        assert row.supports_json_object is False
        assert row.token_param == "max_completion_tokens"

    def test_token_param_fallback_creates_db_row(self) -> None:
        config = _make_config(model="gpt-oldparam")
        backend = OpenAIBackend(config)
        assert backend._token_param == "max_completion_tokens"

        bad_req = self._bad_request("max_completion_tokens is not valid for this model")
        good_resp = MagicMock()
        good_resp.choices = [MagicMock()]
        good_resp.choices[0].message.content = "[]"
        good_resp.usage = None

        call_count = 0

        def _fake_create(**kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise bad_req
            return good_resp

        with patch("openai.OpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = _fake_create
            backend._call_api("sys", "user", "fake-key")

        assert backend._token_param == "max_tokens"
        row = LLMBackendFallback.objects.get(provider="openai", model="gpt-oldparam", base_url="")
        assert row.token_param == "max_tokens"

    def test_subsequent_instance_loads_persisted_json_fallback(self) -> None:
        """A second OpenAIBackend for the same model skips the bad first request."""
        config = _make_config(model="gpt-probe")
        # Pre-seed the DB row as if a previous run already discovered the fallback.
        LLMBackendFallbackFactory(
            provider="openai",
            model="gpt-probe",
            base_url="",
            token_param="max_completion_tokens",
            supports_json_object=False,
        )
        backend = OpenAIBackend(config)
        assert backend._supports_json_object is False

        good_resp = MagicMock()
        good_resp.choices = [MagicMock()]
        good_resp.choices[0].message.content = "[]"
        good_resp.usage = None

        with patch("openai.OpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = good_resp
            backend._call_api("sys", "user", "fake-key")

        # Should have been called exactly once — no retry wasted.
        assert mock_client.chat.completions.create.call_count == 1
        # Confirm response_format was NOT sent because supports_json_object=False.
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert "response_format" not in call_kwargs


@pytest.mark.django_db
class TestClearLlmFallbacksCommand:
    """Management command deletes all LLMBackendFallback rows."""

    def test_clears_all_rows(self) -> None:
        from django.core.management import call_command

        LLMBackendFallbackFactory.create_batch(3)
        assert LLMBackendFallback.objects.count() == 3

        call_command("clear_llm_fallbacks", yes=True)

        assert LLMBackendFallback.objects.count() == 0

    def test_no_op_when_empty(self) -> None:
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("clear_llm_fallbacks", yes=True, stdout=out)
        assert "No LLM backend fallback rows" in out.getvalue()

    def test_counts_are_reported(self) -> None:
        from io import StringIO

        from django.core.management import call_command

        LLMBackendFallbackFactory.create_batch(2)
        out = StringIO()
        call_command("clear_llm_fallbacks", yes=True, stdout=out)
        assert "2" in out.getvalue()
