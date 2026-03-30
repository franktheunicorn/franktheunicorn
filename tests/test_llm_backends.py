"""Tests for LLM backend registry, base types, and individual backends."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from franktheunicorn.config.models import LLMBackendConfig
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.backends.base import (
    PRContext,
    ReviewFinding,
    parse_llm_response,
)
from franktheunicorn.review.backends.stub_backend import StubBackend


def _make_pr_context(**overrides: object) -> PRContext:
    defaults = {
        "pr_title": "Fix flaky test",
        "pr_body": "Fixes race condition",
        "pr_author": "alice",
        "pr_number": 42,
        "project_name": "apache/spark",
        "review_context": "ASF governance",
        "review_style": "direct but kind",
        "tone": "constructive",
        "test_expectations": "tests required",
        "governance": "asf",
        "anti_patterns": [],
    }
    defaults.update(overrides)
    return PRContext(**defaults)  # type: ignore[arg-type]


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
        ctx = _make_pr_context()
        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert len(findings) > 0
        assert all(isinstance(f, ReviewFinding) for f in findings)

    def test_deterministic(self) -> None:
        config = LLMBackendConfig(provider="stub")
        backend = StubBackend(config)
        ctx = _make_pr_context()
        f1 = backend.generate_findings(_SAMPLE_DIFF, ctx)
        f2 = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert len(f1) == len(f2)
        assert f1[0].body == f2[0].body

    def test_extracts_file_paths_from_diff(self) -> None:
        config = LLMBackendConfig(provider="stub")
        backend = StubBackend(config)
        ctx = _make_pr_context()
        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert findings[0].file_path == "src/main.py"


class TestClaudeBackend:
    def test_missing_api_key_returns_empty(self) -> None:
        config = LLMBackendConfig(provider="claude", api_key_env="NONEXISTENT_KEY_12345")
        from franktheunicorn.review.backends.claude_backend import ClaudeBackend

        backend = ClaudeBackend(config)
        ctx = _make_pr_context()
        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert findings == []

    @patch.dict("os.environ", {"TEST_ANTHROPIC_KEY": "sk-test"})
    def test_successful_call(self) -> None:
        config = LLMBackendConfig(provider="claude", api_key_env="TEST_ANTHROPIC_KEY")
        from franktheunicorn.review.backends.claude_backend import ClaudeBackend

        backend = ClaudeBackend(config)
        ctx = _make_pr_context()

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
        ctx = _make_pr_context()
        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert findings == []

    @patch.dict("os.environ", {"TEST_OPENAI_KEY": "sk-test"})
    def test_successful_call(self) -> None:
        config = LLMBackendConfig(provider="openai", api_key_env="TEST_OPENAI_KEY")
        from franktheunicorn.review.backends.openai_backend import OpenAIBackend

        backend = OpenAIBackend(config)
        ctx = _make_pr_context()

        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(
            {"findings": [{"file_path": "a.py", "title": "T", "body": "B", "severity": "high"}]}
        )
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        with patch("openai.OpenAI") as mock_client_cls:
            mock_client_cls.return_value.chat.completions.create.return_value = mock_response
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)

        assert len(findings) == 1
        assert findings[0].confidence == 0.8  # high -> 0.8


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
            ctx = _make_pr_context()
            findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
            assert findings == []


class TestOllamaBackend:
    @patch("ollama.Client")
    def test_successful_call(self, mock_client_cls: MagicMock) -> None:
        config = LLMBackendConfig(provider="ollama", base_url="http://localhost:11434")
        from franktheunicorn.review.backends.ollama_backend import OllamaBackend

        backend = OllamaBackend(config)
        ctx = _make_pr_context()

        mock_response = MagicMock()
        mock_response.message.content = json.dumps(
            {"findings": [{"file_path": "b.py", "title": "T", "body": "B", "severity": "nit"}]}
        )
        mock_client_cls.return_value.chat.return_value = mock_response

        findings = backend.generate_findings(_SAMPLE_DIFF, ctx)
        assert len(findings) == 1
        assert findings[0].confidence == 0.3  # nit -> 0.3
