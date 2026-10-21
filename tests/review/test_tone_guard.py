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
        mock_backend.metered_call.return_value = "Consider refactoring this section."
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
        mock_backend.metered_call.side_effect = RuntimeError("API down")
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
        mock_backend.metered_call.return_value = ""
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, rewritten = apply_tone_guard(finding, ctx, backend_config=config)

        assert result.body == "Original."
        assert rewritten is False

    def test_salvages_json_wrapped_rewrite(self) -> None:
        # A weaker model wraps the rewrite in a JSON object despite being asked
        # for plain text; the prose should be salvaged, not posted as a blob.
        finding = ReviewFinding(body="Bad code.", file_path="test.py", line_number=3)
        ctx = make_pr_context()

        mock_backend = MagicMock()
        mock_backend.metered_call.return_value = '{"comment": "Consider refactoring this."}'
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, rewritten = apply_tone_guard(finding, ctx, backend_config=config)

        assert rewritten is True
        assert result.body == "Consider refactoring this."

    def test_empty_json_object_rewrite_keeps_original(self) -> None:
        # "{}" salvages to empty, which must fall back to the original body
        # (and report rewritten=False) rather than blanking the finding.
        finding = ReviewFinding(body="Original body.", file_path="test.py")
        ctx = make_pr_context()

        mock_backend = MagicMock()
        mock_backend.metered_call.return_value = "{}"
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, rewritten = apply_tone_guard(finding, ctx, backend_config=config)

        assert result.body == "Original body."
        assert rewritten is False

    def test_meta_response_keeps_original(self) -> None:
        # The model answers conversationally instead of rewriting; the original
        # (already in-voice) body must be kept, not the meta-response.
        finding = ReviewFinding(body="This might miss the subqueries.", file_path="a.scala")
        ctx = make_pr_context()

        mock_backend = MagicMock()
        mock_backend.metered_call.return_value = (
            "The user provided a prompt that does not contain a comment to rewrite. "
            "Please provide the comment you would like me to edit."
        )
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, rewritten = apply_tone_guard(finding, ctx, backend_config=config)

        assert result.body == "This might miss the subqueries."
        assert rewritten is False

    def test_dropped_leading_subject_keeps_original(self) -> None:
        # The model returns the tail of the comment with its subject sliced off.
        # The fragment must be rejected and the original (in-voice) body kept.
        finding = ReviewFinding(
            body="The `getent` command uses `$X` inside a string passed to `ssh`.",
            file_path="setup.sh",
        )
        ctx = make_pr_context()

        mock_backend = MagicMock()
        mock_backend.metered_call.return_value = (
            "command uses `$X` inside a string passed to `ssh`."
        )
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, rewritten = apply_tone_guard(finding, ctx, backend_config=config)

        assert result.body == "The `getent` command uses `$X` inside a string passed to `ssh`."
        assert rewritten is False

    def test_genuine_rephrase_is_not_a_dropped_subject(self) -> None:
        # A real rewrite replaces words rather than slicing off the front; it
        # must still be accepted even though it is shorter than the original.
        from franktheunicorn.review.tone_guard import _dropped_leading_subject

        assert not _dropped_leading_subject(
            "This is terrible code that nobody should ever write.",
            "Consider refactoring this section.",
        )
        assert _dropped_leading_subject("The getent command uses $X here.", "command uses $X here.")

    def test_please_add_a_test_is_not_meta(self) -> None:
        # Guard against false positives: a legitimate "please add a test"
        # comment must survive the meta-response filter.
        finding = ReviewFinding(body="orig", file_path="a.scala")
        ctx = make_pr_context()

        mock_backend = MagicMock()
        mock_backend.metered_call.return_value = "Could you please add a test for the null path?"
        mock_backend._resolve_api_key.return_value = "key"

        with patch("franktheunicorn.review.backends.get_backend", return_value=mock_backend):
            from franktheunicorn.config.models import LLMBackendConfig

            config = LLMBackendConfig(provider="stub")
            result, rewritten = apply_tone_guard(finding, ctx, backend_config=config)

        assert result.body == "Could you please add a test for the null path?"
        assert rewritten is True


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
        mock_backend.metered_call.side_effect = ["Good1", "Good2"]
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
        mock_backend.metered_call.side_effect = ["Good1", RuntimeError("API down")]
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

    def test_does_not_formalize_the_operators_voice(self) -> None:
        prompt = _build_tone_prompt(make_pr_context())
        assert "Do not lengthen" in prompt
        assert "This seems suspicious" in prompt
