"""Tests for Tone Guard rewrite pass (§4)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.tone_guard import (
    _build_tone_prompt,
    apply_tone_guard,
    apply_tone_guard_batch,
)
from tests.conftest import make_pr_context


class TestApplyToneGuard:
    def test_returns_original_when_no_backend(self) -> None:
        finding = ReviewFinding(body="This is wrong.", file_path="test.py")
        ctx = make_pr_context()
        result, rewritten = apply_tone_guard(finding, ctx, backend_config=None)
        assert result.body == "This is wrong."
        assert rewritten is False

    def test_rewrites_body_and_preserves_original(self) -> None:
        finding = ReviewFinding(
            body="This is terrible code.",
            file_path="test.py",
            line_number=10,
            confidence=0.8,
            severity="important",
        )
        ctx = make_pr_context(tone="constructive")

        mock_backend = MagicMock()
        mock_backend._call_api.return_value = "Consider refactoring this section."
        mock_backend._resolve_api_key.return_value = "test-key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, rewritten = apply_tone_guard(finding, ctx, backend_config=config)

        assert rewritten is True
        assert result.body == "Consider refactoring this section."
        assert result.title == "This is terrible code."  # original preserved
        assert result.file_path == "test.py"
        assert result.line_number == 10
        assert result.confidence == 0.8

    def test_returns_original_on_api_failure(self) -> None:
        finding = ReviewFinding(body="Bad code.", file_path="test.py")
        ctx = make_pr_context()

        mock_backend = MagicMock()
        mock_backend._call_api.side_effect = RuntimeError("API down")
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, rewritten = apply_tone_guard(finding, ctx, backend_config=config)

        assert result.body == "Bad code."
        # The failure path must report rewritten=False so drafts are not
        # marked tone_guard_applied (auto-poster Gate 3 depends on this).
        assert rewritten is False

    def test_returns_original_on_empty_response(self) -> None:
        finding = ReviewFinding(body="Original.", file_path="test.py")
        ctx = make_pr_context()

        mock_backend = MagicMock()
        mock_backend._call_api.return_value = ""
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, rewritten = apply_tone_guard(finding, ctx, backend_config=config)

        assert result.body == "Original."
        assert rewritten is False


class TestApplyToneGuardBatch:
    def test_no_backend_returns_originals(self) -> None:
        findings = [
            ReviewFinding(body="A", file_path="a.py"),
            ReviewFinding(body="B", file_path="b.py"),
        ]
        ctx = make_pr_context()
        result, flags = apply_tone_guard_batch(findings, ctx, backend_config=None)
        assert len(result) == 2
        assert result[0].body == "A"
        assert result[1].body == "B"
        assert flags == [False, False]

    def test_batch_applies_to_all(self) -> None:
        findings = [
            ReviewFinding(body="Bad1", file_path="a.py"),
            ReviewFinding(body="Bad2", file_path="b.py"),
        ]
        ctx = make_pr_context()

        mock_backend = MagicMock()
        mock_backend._call_api.side_effect = ["Good1", "Good2"]
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, flags = apply_tone_guard_batch(findings, ctx, backend_config=config)

        assert result[0].body == "Good1"
        assert result[1].body == "Good2"
        assert flags == [True, True]

    def test_batch_flags_partial_failure(self) -> None:
        findings = [
            ReviewFinding(body="Bad1", file_path="a.py"),
            ReviewFinding(body="Bad2", file_path="b.py"),
        ]
        ctx = make_pr_context()

        mock_backend = MagicMock()
        mock_backend._call_api.side_effect = ["Good1", RuntimeError("API down")]
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, flags = apply_tone_guard_batch(findings, ctx, backend_config=config)

        assert result[0].body == "Good1"
        assert result[1].body == "Bad2"  # original kept on failure
        assert flags == [True, False]


class TestToneGuardPersonality:
    def test_external_voice_injected_into_tone_prompt(self) -> None:
        ctx = make_pr_context(
            personality_external_voice="Drop all character references — no unicorn."
        )
        prompt = _build_tone_prompt(ctx)
        assert "Drop all character references" in prompt

    def test_no_external_voice_omitted(self) -> None:
        ctx = make_pr_context(personality_external_voice="")
        prompt = _build_tone_prompt(ctx)
        assert "External voice guidance" not in prompt
