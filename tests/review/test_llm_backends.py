"""Tests for LLM backend registry, base types, and individual backends."""

from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.backends.base import (
    ReviewFinding,
    parse_llm_response,
    parse_llm_review,
)
from franktheunicorn.review.backends.stub_backend import StubBackend
from tests.conftest import make_pr_context


def _cffi_available() -> bool:
    try:
        importlib.import_module("_cffi_backend")
        return True
    except (ImportError, ModuleNotFoundError):
        return False


_SAMPLE_DIFF = """\
diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,4 @@
 import os
+import sys

 def main():
     pass
"""


class TestParseResponse:
    def test_empty_string(self) -> None:
        assert parse_llm_response("") == []

    def test_valid_json_array(self) -> None:
        data = [
            {
                "file_path": "src/main.py",
                "line_number": 2,
                "title": "Unused import",
                "body": "sys is imported but never used.",
                "severity": "low",
            }
        ]
        findings = parse_llm_response(json.dumps(data))
        assert len(findings) == 1
        assert findings[0].file_path == "src/main.py"
        assert findings[0].line_number == 2
        assert findings[0].confidence == 0.4  # low severity -> 0.4

    def test_valid_json_object_with_findings_key(self) -> None:
        data = {
            "findings": [
                {
                    "file_path": "a.py",
                    "title": "Test",
                    "body": "Test body",
                    "severity": "critical",
                }
            ]
        }
        findings = parse_llm_response(json.dumps(data))
        assert len(findings) == 1
        assert findings[0].confidence == 0.9  # critical -> 0.9

    def test_invalid_json(self) -> None:
        assert parse_llm_response("not json at all") == []

    def test_extracts_json_with_prose_preamble(self) -> None:
        """Models without response_format enforcement may add a chatty preamble."""
        data = {"findings": [{"file_path": "x.py", "title": "T", "body": "B", "severity": "low"}]}
        raw = "Sure, here is the review: " + json.dumps(data)
        findings = parse_llm_response(raw)
        assert len(findings) == 1
        assert findings[0].file_path == "x.py"

    def test_extracts_json_with_trailing_prose(self) -> None:
        data = [{"file_path": "y.py", "title": "T", "body": "B", "severity": "high"}]
        raw = json.dumps(data) + "\n\nLet me know if you have questions!"
        findings = parse_llm_response(raw)
        assert len(findings) == 1
        assert findings[0].file_path == "y.py"

    def test_extracts_json_with_prose_on_both_sides(self) -> None:
        data = {"findings": [{"file_path": "z.py", "title": "T", "body": "B"}]}
        raw = f"Here you go:\n{json.dumps(data)}\nHope that helps."
        findings = parse_llm_response(raw)
        assert len(findings) == 1
        assert findings[0].file_path == "z.py"

    def test_markdown_code_fences(self) -> None:
        raw = '```json\n[{"file_path":"x.py","title":"T","body":"B","severity":"nit"}]\n```'
        findings = parse_llm_response(raw)
        assert len(findings) == 1
        assert findings[0].severity == "nit"
        assert findings[0].confidence == 0.3

    def test_skips_invalid_items(self) -> None:
        data = [
            {"file_path": "ok.py", "title": "Good", "body": "Body", "severity": "medium"},
            "not a dict",
            {"file_path": "ok2.py", "title": "Also good", "body": "Body2"},
        ]
        findings = parse_llm_response(json.dumps(data))
        assert len(findings) == 2

    def test_confidence_from_unknown_severity(self) -> None:
        data = [
            {
                "file_path": "a.py",
                "title": "T",
                "body": "B",
                "severity": "unknown",
                "confidence": 0.75,
            }
        ]
        findings = parse_llm_response(json.dumps(data))
        assert len(findings) == 1
        assert findings[0].confidence == 0.75


class TestParseReview:
    def test_extracts_overall_vibe_and_findings(self) -> None:
        data = {
            "overall_vibe": "Looks fine, minor nits.",
            "findings": [
                {"file_path": "a.py", "title": "T", "body": "B", "severity": "nit"},
            ],
        }
        result = parse_llm_review(json.dumps(data))
        assert result.overall_vibe == "Looks fine, minor nits."
        assert len(result.findings) == 1
        assert result.findings[0].file_path == "a.py"

    def test_array_only_response_has_no_vibe(self) -> None:
        data = [{"file_path": "a.py", "title": "T", "body": "B"}]
        result = parse_llm_review(json.dumps(data))
        assert result.overall_vibe == ""
        assert len(result.findings) == 1

    def test_empty_response(self) -> None:
        result = parse_llm_review("")
        assert result.overall_vibe == ""
        assert result.findings == []

    def test_non_string_vibe_ignored(self) -> None:
        data = {"overall_vibe": 42, "findings": []}
        result = parse_llm_review(json.dumps(data))
        assert result.overall_vibe == ""

    def test_stub_backend_emits_vibe(self) -> None:
        config = LLMBackendConfig(provider="stub")
        backend = get_backend(config)
        ctx = make_pr_context()
        result = backend.generate_review(_SAMPLE_DIFF, ctx)
        assert result.overall_vibe.startswith("Overall vibes:")
        assert len(result.findings) >= 1


class TestGetBackend:
    def test_stub_backend(self) -> None:
        config = LLMBackendConfig(provider="stub")
        backend = get_backend(config)
        assert isinstance(backend, StubBackend)

    def test_unknown_falls_back_to_stub(self) -> None:
        config = LLMBackendConfig(provider="unknown-provider")
        backend = get_backend(config)
        assert isinstance(backend, StubBackend)

    def test_claude_backend(self) -> None:
        config = LLMBackendConfig(provider="claude")
        backend = get_backend(config)
        assert type(backend).__name__ == "ClaudeBackend"

    def test_openai_backend(self) -> None:
        config = LLMBackendConfig(provider="openai")
        backend = get_backend(config)
        assert type(backend).__name__ == "OpenAIBackend"

    @pytest.mark.skipif(not _cffi_available(), reason="google-auth requires _cffi_backend")
    def test_gemini_backend(self) -> None:
        config = LLMBackendConfig(provider="gemini")
        backend = get_backend(config)
        assert type(backend).__name__ == "GeminiBackend"

    def test_ollama_backend(self) -> None:
        config = LLMBackendConfig(provider="ollama")
        backend = get_backend(config)
        assert type(backend).__name__ == "OllamaBackend"


class TestStubBackend:
    def test_generates_findings(self) -> None:
        config = LLMBackendConfig(provider="stub")
        backend = StubBackend(config)
        ctx = make_pr_context()
        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert len(findings) > 0
        assert all(isinstance(f, ReviewFinding) for f in findings)

    def test_deterministic(self) -> None:
        config = LLMBackendConfig(provider="stub")
        backend = StubBackend(config)
        ctx = make_pr_context()
        f1 = backend.generate_findings(_SAMPLE_DIFF, ctx)
        f2 = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert len(f1) == len(f2)
        assert f1[0].body == f2[0].body

    def test_extracts_file_paths_from_diff(self) -> None:
        config = LLMBackendConfig(provider="stub")
        backend = StubBackend(config)
        ctx = make_pr_context()
        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert findings[0].file_path == "src/main.py"


class TestClaudeBackend:
    def test_missing_api_key_returns_empty(self) -> None:
        config = LLMBackendConfig(provider="claude", api_key_env="NONEXISTENT_KEY_12345")
        from franktheunicorn.review.backends.claude_backend import ClaudeBackend

        backend = ClaudeBackend(config)
        ctx = make_pr_context()
        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert findings == []

    @patch.dict("os.environ", {"TEST_ANTHROPIC_KEY": "sk-test"})
    def test_successful_call(self) -> None:
        config = LLMBackendConfig(provider="claude", api_key_env="TEST_ANTHROPIC_KEY")
        from franktheunicorn.review.backends.claude_backend import ClaudeBackend

        backend = ClaudeBackend(config)
        ctx = make_pr_context()

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "findings": [
                            {
                                "file_path": "src/main.py",
                                "line_number": 2,
                                "title": "Unused import",
                                "body": "sys imported but unused",
                                "severity": "low",
                            }
                        ]
                    }
                )
            )
        ]

        with patch("anthropic.Anthropic") as mock_client_cls:
            mock_client_cls.return_value.messages.create.return_value = mock_response
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)

        assert len(findings) == 1
        assert findings[0].file_path == "src/main.py"


class TestOpenAIBackend:
    def test_missing_api_key_returns_empty(self) -> None:
        config = LLMBackendConfig(provider="openai", api_key_env="NONEXISTENT_KEY_12345")
        from franktheunicorn.review.backends.openai_backend import OpenAIBackend

        backend = OpenAIBackend(config)
        ctx = make_pr_context()
        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert findings == []

    @patch.dict("os.environ", {"TEST_OPENAI_KEY": "sk-test"})
    def test_successful_call(self) -> None:
        config = LLMBackendConfig(provider="openai", api_key_env="TEST_OPENAI_KEY")
        from franktheunicorn.review.backends.openai_backend import OpenAIBackend

        backend = OpenAIBackend(config)
        ctx = make_pr_context()

        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(
            {"findings": [{"file_path": "a.py", "title": "T", "body": "B", "severity": "high"}]}
        )
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        with patch("openai.OpenAI") as mock_client_cls:
            create = mock_client_cls.return_value.chat.completions.create
            create.return_value = mock_response
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
            # Default is the modern parameter.
            kwargs = create.call_args.kwargs
            assert "max_completion_tokens" in kwargs
            assert "max_tokens" not in kwargs

        assert len(findings) == 1
        assert findings[0].confidence == 0.8  # high -> 0.8

    @patch.dict("os.environ", {"TEST_OPENAI_KEY": "sk-test"})
    def test_falls_back_to_max_tokens_for_legacy_server(self) -> None:
        """Legacy vLLM servers reject `max_completion_tokens`; retry with `max_tokens`."""
        import openai

        config = LLMBackendConfig(provider="openai", api_key_env="TEST_OPENAI_KEY")
        from franktheunicorn.review.backends.openai_backend import OpenAIBackend

        backend = OpenAIBackend(config)
        ctx = make_pr_context()

        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(
            {"findings": [{"file_path": "a.py", "title": "T", "body": "B", "severity": "low"}]}
        )
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        bad_request = openai.BadRequestError(
            message="unrecognized field: max_completion_tokens",
            response=MagicMock(),
            body={"message": "unrecognized field: max_completion_tokens"},
        )

        with patch("openai.OpenAI") as mock_client_cls:
            create = mock_client_cls.return_value.chat.completions.create
            create.side_effect = [bad_request, mock_response]
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)

            assert create.call_count == 2
            first_kwargs = create.call_args_list[0].kwargs
            second_kwargs = create.call_args_list[1].kwargs
            assert "max_completion_tokens" in first_kwargs
            assert "max_tokens" in second_kwargs
            assert "max_completion_tokens" not in second_kwargs

        assert len(findings) == 1
        # Fallback is cached for subsequent calls on this backend instance.
        assert backend._token_param == "max_tokens"

    @patch.dict("os.environ", {"TEST_OPENAI_KEY": "sk-test"})
    def test_falls_back_when_response_format_unsupported(self) -> None:
        """Servers that reject response_format=json_object should retry without it."""
        import openai

        config = LLMBackendConfig(provider="openai", api_key_env="TEST_OPENAI_KEY")
        from franktheunicorn.review.backends.openai_backend import OpenAIBackend

        backend = OpenAIBackend(config)
        ctx = make_pr_context()

        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(
            {"findings": [{"file_path": "a.py", "title": "T", "body": "B", "severity": "low"}]}
        )
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        bad_request = openai.BadRequestError(
            message="json_object response format is not supported",
            response=MagicMock(),
            body={"message": "json_object response format is not supported"},
        )

        with patch("openai.OpenAI") as mock_client_cls:
            create = mock_client_cls.return_value.chat.completions.create
            create.side_effect = [bad_request, mock_response]
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)

            assert create.call_count == 2
            first_kwargs = create.call_args_list[0].kwargs
            second_kwargs = create.call_args_list[1].kwargs
            assert first_kwargs.get("response_format") == {"type": "json_object"}
            assert "response_format" not in second_kwargs

        assert len(findings) == 1
        # The degradation is cached for subsequent calls on this backend instance.
        assert backend._supports_json_object is False
        # Without response_format enforcement, the system prompt gets a
        # stronger JSON-only reminder appended.
        retry_system = second_kwargs["messages"][0]["content"]
        assert "ONLY the JSON" in retry_system
        first_system = first_kwargs["messages"][0]["content"]
        assert "ONLY the JSON" not in first_system

    @patch.dict("os.environ", {"TEST_OPENAI_KEY": "sk-test"})
    def test_falls_back_for_both_response_format_and_token_param(self) -> None:
        """A server can be old enough to reject both quirks; both should degrade."""
        import openai

        config = LLMBackendConfig(provider="openai", api_key_env="TEST_OPENAI_KEY")
        from franktheunicorn.review.backends.openai_backend import OpenAIBackend

        backend = OpenAIBackend(config)
        ctx = make_pr_context()

        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(
            {"findings": [{"file_path": "a.py", "title": "T", "body": "B", "severity": "low"}]}
        )
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        format_err = openai.BadRequestError(
            message="response_format is not supported",
            response=MagicMock(),
            body={"message": "response_format is not supported"},
        )
        token_err = openai.BadRequestError(
            message="unrecognized field: max_completion_tokens",
            response=MagicMock(),
            body={"message": "unrecognized field: max_completion_tokens"},
        )

        with patch("openai.OpenAI") as mock_client_cls:
            create = mock_client_cls.return_value.chat.completions.create
            create.side_effect = [format_err, token_err, mock_response]
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)

            assert create.call_count == 3
            final_kwargs = create.call_args_list[2].kwargs
            assert "response_format" not in final_kwargs
            assert "max_tokens" in final_kwargs
            assert "max_completion_tokens" not in final_kwargs

        assert len(findings) == 1
        assert backend._supports_json_object is False
        assert backend._token_param == "max_tokens"

    @patch.dict("os.environ", {"TEST_OPENAI_KEY": "sk-test"})
    def test_unrelated_bad_request_is_not_retried(self) -> None:
        """A 400 unrelated to the token-param field must propagate, not loop."""
        import openai

        config = LLMBackendConfig(provider="openai", api_key_env="TEST_OPENAI_KEY")
        from franktheunicorn.review.backends.openai_backend import OpenAIBackend

        backend = OpenAIBackend(config)
        ctx = make_pr_context()

        bad_request = openai.BadRequestError(
            message="model not found",
            response=MagicMock(),
            body={"message": "model not found"},
        )

        with patch("openai.OpenAI") as mock_client_cls:
            create = mock_client_cls.return_value.chat.completions.create
            create.side_effect = bad_request
            # generate_findings swallows exceptions and returns [].
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
            assert create.call_count == 1
            assert findings == []


class TestGeminiBackend:
    def test_missing_api_key_returns_empty(self) -> None:
        # Mock the google.genai imports to avoid dependency issues in test env.
        mock_genai = MagicMock()
        mock_types = MagicMock()
        with patch.dict(
            "sys.modules",
            {"google": MagicMock(), "google.genai": mock_genai, "google.genai.types": mock_types},
        ):
            config = LLMBackendConfig(provider="gemini", api_key_env="NONEXISTENT_KEY_12345")
            from franktheunicorn.review.backends.gemini_backend import GeminiBackend

            backend = GeminiBackend(config)
            ctx = make_pr_context()
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
            assert findings == []


class TestOllamaBackend:
    @patch("ollama.Client")
    def test_successful_call(self, mock_client_cls: MagicMock) -> None:
        config = LLMBackendConfig(provider="ollama", base_url="http://localhost:11434")
        from franktheunicorn.review.backends.ollama_backend import OllamaBackend

        backend = OllamaBackend(config)
        ctx = make_pr_context()

        mock_response = MagicMock()
        mock_response.message.content = json.dumps(
            {"findings": [{"file_path": "b.py", "title": "T", "body": "B", "severity": "nit"}]}
        )
        mock_client_cls.return_value.chat.return_value = mock_response

        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert len(findings) == 1
        assert findings[0].confidence == 0.3  # nit -> 0.3


class TestCostEstimation:
    def test_estimate_cost_claude(self) -> None:
        from franktheunicorn.review.backends.base import _estimate_cost

        cost = _estimate_cost("claude", "claude-sonnet-4-20250514", 1000, 500)
        assert cost > 0
        assert isinstance(cost, float)

    def test_estimate_cost_stub(self) -> None:
        from franktheunicorn.review.backends.base import _estimate_cost

        cost = _estimate_cost("stub", "stub", 1000, 500)
        assert cost == 0.0

    def test_estimate_cost_unknown_provider(self) -> None:
        from franktheunicorn.review.backends.base import _estimate_cost

        cost = _estimate_cost("unknown", "model", 1000, 500)
        assert cost > 0  # uses default rates

    def test_record_cost_no_tokens(self) -> None:
        from franktheunicorn.review.backends.base import BaseLLMBackend

        config = LLMBackendConfig(provider="stub")
        backend = BaseLLMBackend(config)
        backend._last_tokens_in = 0
        backend._last_tokens_out = 0
        # Should not raise
        backend.record_cost(project_id=1, pr_id=1)

    def test_record_cost_no_project(self) -> None:
        from franktheunicorn.review.backends.base import BaseLLMBackend

        config = LLMBackendConfig(provider="stub")
        backend = BaseLLMBackend(config)
        backend._last_tokens_in = 100
        backend._last_tokens_out = 50
        # Should return early when project_id is None
        backend.record_cost(project_id=None, pr_id=None)


class TestModelRecommendations:
    """Tests for hardware-aware model recommendations."""

    def test_recommend_gguf_model_returns_filename_and_reason(self) -> None:
        from franktheunicorn.review.backends.ollama_backend import recommend_gguf_model

        with patch(
            "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
            return_value=("qwen2.5-coder:14b", "12GB VRAM available"),
        ):
            gguf, reason = recommend_gguf_model()
        assert gguf == "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf"
        assert reason == "12GB VRAM available"

    def test_recommend_gguf_model_for_each_tier(self) -> None:
        from franktheunicorn.review.backends.ollama_backend import recommend_gguf_model

        cases = {
            "qwen2.5-coder:3b": "Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf",
            "qwen2.5-coder:7b": "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
            "qwen2.5-coder:14b": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf",
            "qwen2.5-coder:32b": "Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf",
        }
        for ollama_model, expected_gguf in cases.items():
            with patch(
                "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
                return_value=(ollama_model, "test"),
            ):
                gguf, _ = recommend_gguf_model()
            assert gguf == expected_gguf, f"Mismatch for {ollama_model}"

    def test_recommend_gguf_model_unknown_falls_back(self) -> None:
        from franktheunicorn.review.backends.ollama_backend import recommend_gguf_model

        with patch(
            "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
            return_value=("unknown-model:1b", "weird hardware"),
        ):
            gguf, _ = recommend_gguf_model()
        # Falls back to the smallest known model so the user gets something usable.
        assert gguf == "Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf"

    def test_recommend_hf_model_returns_id_and_reason(self) -> None:
        from franktheunicorn.review.backends.ollama_backend import recommend_hf_model

        with patch(
            "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
            return_value=("qwen2.5-coder:14b", "12GB VRAM available"),
        ):
            hf, reason = recommend_hf_model()
        assert hf == "Qwen/Qwen2.5-Coder-14B-Instruct"
        assert reason == "12GB VRAM available"

    def test_recommend_hf_model_for_each_tier(self) -> None:
        from franktheunicorn.review.backends.ollama_backend import recommend_hf_model

        cases = {
            "qwen2.5-coder:3b": "Qwen/Qwen2.5-Coder-3B-Instruct",
            "qwen2.5-coder:7b": "Qwen/Qwen2.5-Coder-7B-Instruct",
            "qwen2.5-coder:14b": "Qwen/Qwen2.5-Coder-14B-Instruct",
            "qwen2.5-coder:32b": "Qwen/Qwen2.5-Coder-32B-Instruct",
        }
        for ollama_model, expected_hf in cases.items():
            with patch(
                "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
                return_value=(ollama_model, "test"),
            ):
                hf, _ = recommend_hf_model()
            assert hf == expected_hf, f"Mismatch for {ollama_model}"

    def test_recommend_hf_model_unknown_falls_back(self) -> None:
        from franktheunicorn.review.backends.ollama_backend import recommend_hf_model

        with patch(
            "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
            return_value=("unknown-model:1b", "weird hardware"),
        ):
            hf, _ = recommend_hf_model()
        assert hf == "Qwen/Qwen2.5-Coder-3B-Instruct"


class _HttpError(Exception):
    """Minimal HTTP-like exception with a ``status_code`` for testing _log_backend_error."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code} error")
        self.status_code = status_code


class TestLogBackendError:
    """Tests for _log_backend_error: correct level and message for HTTP 4xx."""

    def test_401_logs_at_error_with_auth_hint(self, caplog: pytest.LogCaptureFixture) -> None:
        from franktheunicorn.review.backends.base import _log_backend_error

        with caplog.at_level("ERROR", logger="franktheunicorn"):
            _log_backend_error("ClaudeBackend", _HttpError(401))

        assert any(
            "401" in r.message and "authentication" in r.message.lower() for r in caplog.records
        )
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_403_logs_at_error_with_permission_hint(self, caplog: pytest.LogCaptureFixture) -> None:
        from franktheunicorn.review.backends.base import _log_backend_error

        with caplog.at_level("ERROR", logger="franktheunicorn"):
            _log_backend_error("ClaudeBackend", _HttpError(403))

        assert any("403" in r.message and "permission" in r.message.lower() for r in caplog.records)
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_429_logs_at_warning_with_rate_limit_hint(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from franktheunicorn.review.backends.base import _log_backend_error

        with caplog.at_level("WARNING", logger="franktheunicorn"):
            _log_backend_error("OpenAIBackend", _HttpError(429))

        assert any("429" in r.message and "rate limit" in r.message.lower() for r in caplog.records)
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_other_4xx_logs_at_error_with_status_code(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from franktheunicorn.review.backends.base import _log_backend_error

        with caplog.at_level("ERROR", logger="franktheunicorn"):
            _log_backend_error("GeminiBackend", _HttpError(422))

        assert any("422" in r.message for r in caplog.records)
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_no_status_code_logs_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        from franktheunicorn.review.backends.base import _log_backend_error

        with caplog.at_level("ERROR", logger="franktheunicorn"):
            _log_backend_error("OllamaBackend", ValueError("connection refused"))

        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_generate_findings_logs_401_on_api_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """generate_findings should emit an actionable 401 log, not a raw traceback."""
        import os

        from franktheunicorn.review.backends.claude_backend import ClaudeBackend

        config = LLMBackendConfig(provider="claude", api_key_env="TEST_CLAUDE_401_KEY")
        backend = ClaudeBackend(config)
        ctx = make_pr_context()

        with (
            patch.dict(os.environ, {"TEST_CLAUDE_401_KEY": "sk-bad"}),
            patch("anthropic.Anthropic") as mock_cls,
            caplog.at_level("ERROR", logger="franktheunicorn"),
        ):
            mock_cls.return_value.messages.create.side_effect = _HttpError(401)
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)

        assert findings == []
        assert any(
            "401" in r.message and "authentication" in r.message.lower() for r in caplog.records
        )
