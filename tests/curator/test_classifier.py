"""Tests for the comment classifier."""

from __future__ import annotations

from franktheunicorn.curator.classifier import (
    CATEGORIES,
    TONE_FLAGS,
    ClassifiedComment,
    _keyword_category,
    _keyword_tone_flags,
    classify_comments,
)
from tests.curator.helpers import make_raw_comment as _make_comment


class TestKeywordCategory:
    def test_correctness(self) -> None:
        assert _keyword_category("This is a bug, it will crash") == "correctness"

    def test_style(self) -> None:
        assert _keyword_category("Fix the formatting and indent here") == "style"

    def test_architectural(self) -> None:
        assert (
            _keyword_category("This coupling between modules is a design issue, consider refactor")
            == "architectural"
        )

    def test_test_coverage(self) -> None:
        assert _keyword_category("Please add a unit test and assert") == "test-coverage"

    def test_naming(self) -> None:
        assert _keyword_category("The variable name is misleading, rename it") == "naming"

    def test_security(self) -> None:
        assert _keyword_category("This has an injection vulnerability") == "security"

    def test_other_for_generic(self) -> None:
        assert _keyword_category("Looks great, ship it!") == "other"

    def test_returns_valid_category(self) -> None:
        for body in [
            "bug fix crash error",
            "style format indent",
            "architecture design refactor",
            "add test coverage assert",
            "rename variable name",
            "security vulnerability injection",
            "hello world",
        ]:
            assert _keyword_category(body) in CATEGORIES


class TestKeywordToneFlags:
    def test_abrasive(self) -> None:
        flags = _keyword_tone_flags("This is terrible code, obviously wrong")
        assert "abrasive" in flags

    def test_snarky(self) -> None:
        flags = _keyword_tone_flags("Did you even read the docs?")
        assert "snarky" in flags

    def test_pedantic(self) -> None:
        flags = _keyword_tone_flags("Actually, technically this is wrong")
        assert "pedantic" in flags

    def test_condescending(self) -> None:
        flags = _keyword_tone_flags("This is a simple mistake, very trivial")
        assert "condescending" in flags

    def test_no_flags_for_neutral(self) -> None:
        flags = _keyword_tone_flags("Consider using a context manager here")
        assert flags == []

    def test_multiple_flags(self) -> None:
        flags = _keyword_tone_flags("This is terrible. Did you even test? Actually per the spec...")
        assert len(flags) >= 2

    def test_returns_valid_flags(self) -> None:
        flags = _keyword_tone_flags("terrible obviously stupid actually trivial junior")
        for flag in flags:
            assert flag in TONE_FLAGS


class TestClassifyComments:
    def test_classifies_with_keywords_by_default(self) -> None:
        comments = [
            _make_comment("This bug will crash in production"),
            _make_comment("Fix the indent and formatting style"),
        ]

        result = classify_comments(comments)

        assert len(result) == 2
        assert result[0].category == "correctness"
        assert result[1].category == "style"
        assert all(isinstance(c, ClassifiedComment) for c in result)

    def test_classifies_with_keywords_for_stub_backend(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        config = LLMBackendConfig(provider="stub")
        comments = [_make_comment("Add a test for this")]

        result = classify_comments(comments, backend_config=config)

        assert len(result) == 1
        assert result[0].category == "test-coverage"

    def test_preserves_raw_comment(self) -> None:
        comment = _make_comment("Security vulnerability found")
        result = classify_comments([comment])

        assert result[0].raw is comment
        assert result[0].raw.body == "Security vulnerability found"

    def test_detects_tone_flags(self) -> None:
        comment = _make_comment("This is terrible, obviously wrong code")
        result = classify_comments([comment])

        assert result[0].tone_flagged is True
        assert "abrasive" in result[0].tone_flags

    def test_no_tone_flags_for_neutral(self) -> None:
        comment = _make_comment("Consider using a try/except block here")
        result = classify_comments([comment])

        assert result[0].tone_flagged is False
        assert result[0].tone_flags == []

    def test_empty_list(self) -> None:
        assert classify_comments([]) == []


class TestClassifyWithLLM:
    """Tests for the LLM-based classification path (_classify_with_llm)."""

    def _make_backend_config(self, **kwargs):
        from franktheunicorn.config.models import LLMBackendConfig

        defaults = {
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "api_key_env": "ANTHROPIC_API_KEY",
            "max_tokens": 4096,
        }
        defaults.update(kwargs)
        return LLMBackendConfig(**defaults)

    def test_llm_classification_success(self, monkeypatch) -> None:
        """LLM returns valid JSON classifications for a batch."""
        import json
        from types import SimpleNamespace

        llm_response = json.dumps(
            [
                {"index": 0, "category": "correctness", "tone_flags": ["abrasive"]},
                {"index": 1, "category": "style", "tone_flags": []},
            ]
        )

        mock_response = SimpleNamespace(content=[SimpleNamespace(text=llm_response)])

        class MockClient:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                return mock_response

        mock_anthropic = SimpleNamespace(Anthropic=MockClient)
        monkeypatch.setitem(__import__("sys").modules, "anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        comments = [
            _make_comment("This bug will crash"),
            _make_comment("Fix indent style"),
        ]
        config = self._make_backend_config()
        result = classify_comments(comments, backend_config=config)

        assert len(result) == 2
        assert result[0].category == "correctness"
        assert result[0].tone_flagged is True
        assert result[0].tone_flags == ["abrasive"]
        assert result[1].category == "style"
        assert result[1].tone_flagged is False
        assert result[1].tone_flags == []

    def test_llm_falls_back_on_missing_api_key(self, monkeypatch) -> None:
        """When no API key is set, falls back to keyword classification."""
        import sys
        from types import SimpleNamespace

        mock_anthropic = SimpleNamespace(Anthropic=lambda **kw: None)
        monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        comments = [_make_comment("This bug will crash")]
        config = self._make_backend_config(api_key_env="ANTHROPIC_API_KEY")
        result = classify_comments(comments, backend_config=config)

        assert len(result) == 1
        assert result[0].category == "correctness"

    def test_llm_falls_back_on_import_error(self, monkeypatch) -> None:
        """When anthropic package is unavailable, falls back to keywords."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("No module named 'anthropic'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        comments = [_make_comment("This bug will crash")]
        config = self._make_backend_config()
        result = classify_comments(comments, backend_config=config)

        assert len(result) == 1
        assert result[0].category == "correctness"

    def test_llm_falls_back_on_api_error(self, monkeypatch) -> None:
        """When the LLM API raises an exception, falls back to keywords."""
        import sys
        from types import SimpleNamespace

        class MockClient:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                raise RuntimeError("API unavailable")

        mock_anthropic = SimpleNamespace(Anthropic=MockClient)
        monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        comments = [_make_comment("This bug will crash")]
        config = self._make_backend_config()
        result = classify_comments(comments, backend_config=config)

        assert len(result) == 1
        # Falls back to keyword classification
        assert result[0].category == "correctness"

    def test_llm_invalid_json_falls_back(self, monkeypatch) -> None:
        """When LLM returns invalid JSON, falls back to keywords."""
        import sys
        from types import SimpleNamespace

        mock_response = SimpleNamespace(content=[SimpleNamespace(text="not valid json {{{")])

        class MockClient:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                return mock_response

        mock_anthropic = SimpleNamespace(Anthropic=MockClient)
        monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        comments = [_make_comment("This bug will crash")]
        config = self._make_backend_config()
        result = classify_comments(comments, backend_config=config)

        assert len(result) == 1
        assert result[0].category == "correctness"

    def test_llm_invalid_category_becomes_other(self, monkeypatch) -> None:
        """Unknown category from LLM is replaced with 'other'."""
        import json
        import sys
        from types import SimpleNamespace

        llm_response = json.dumps(
            [
                {"index": 0, "category": "nonexistent-category", "tone_flags": []},
            ]
        )

        mock_response = SimpleNamespace(content=[SimpleNamespace(text=llm_response)])

        class MockClient:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                return mock_response

        mock_anthropic = SimpleNamespace(Anthropic=MockClient)
        monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        comments = [_make_comment("Hello world")]
        config = self._make_backend_config()
        result = classify_comments(comments, backend_config=config)

        assert len(result) == 1
        assert result[0].category == "other"

    def test_llm_filters_invalid_tone_flags(self, monkeypatch) -> None:
        """Invalid tone flags from LLM are filtered out."""
        import json
        import sys
        from types import SimpleNamespace

        llm_response = json.dumps(
            [
                {
                    "index": 0,
                    "category": "style",
                    "tone_flags": ["abrasive", "fake-flag", "snarky"],
                },
            ]
        )

        mock_response = SimpleNamespace(content=[SimpleNamespace(text=llm_response)])

        class MockClient:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                return mock_response

        mock_anthropic = SimpleNamespace(Anthropic=MockClient)
        monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        comments = [_make_comment("Fix style")]
        config = self._make_backend_config()
        result = classify_comments(comments, backend_config=config)

        assert result[0].tone_flags == ["abrasive", "snarky"]
        assert result[0].tone_flagged is True

    def test_llm_batching_over_ten(self, monkeypatch) -> None:
        """Comments are batched in groups of 10."""
        import json
        import sys
        from types import SimpleNamespace

        call_count = 0

        def make_response(batch_size):
            return json.dumps(
                [{"index": i, "category": "other", "tone_flags": []} for i in range(batch_size)]
            )

        class MockClient:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                nonlocal call_count
                call_count += 1
                # Count comments in prompt to determine batch size
                prompt = kwargs.get("messages", [{}])[0].get("content", "")
                count = prompt.count("Comment ")
                resp_text = make_response(count)
                return SimpleNamespace(content=[SimpleNamespace(text=resp_text)])

        mock_anthropic = SimpleNamespace(Anthropic=MockClient)
        monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        comments = [_make_comment(f"Comment number {i}") for i in range(15)]
        config = self._make_backend_config()
        result = classify_comments(comments, backend_config=config)

        assert call_count == 2  # 10 + 5
        assert len(result) == 15

    def test_llm_partial_classification_fills_gaps(self, monkeypatch) -> None:
        """If LLM only classifies some comments, remainder falls back to keywords."""
        import json
        import sys
        from types import SimpleNamespace

        # Only classify index 0, skip index 1
        llm_response = json.dumps(
            [
                {"index": 0, "category": "security", "tone_flags": []},
            ]
        )

        mock_response = SimpleNamespace(content=[SimpleNamespace(text=llm_response)])

        class MockClient:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                return mock_response

        mock_anthropic = SimpleNamespace(Anthropic=MockClient)
        monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        comments = [
            _make_comment("Check for injection vulnerability"),
            _make_comment("This bug will crash"),
        ]
        config = self._make_backend_config()
        result = classify_comments(comments, backend_config=config)

        assert len(result) == 2
        # First classified by LLM
        assert result[0].category == "security"
        # Second falls back to keywords
        assert result[1].category == "correctness"

    def test_llm_out_of_range_index_ignored(self, monkeypatch) -> None:
        """LLM response with out-of-range index is ignored, comment falls back."""
        import json
        import sys
        from types import SimpleNamespace

        llm_response = json.dumps(
            [
                {"index": 99, "category": "style", "tone_flags": []},
            ]
        )

        mock_response = SimpleNamespace(content=[SimpleNamespace(text=llm_response)])

        class MockClient:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                return mock_response

        mock_anthropic = SimpleNamespace(Anthropic=MockClient)
        monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-123")

        comments = [_make_comment("This bug will crash")]
        config = self._make_backend_config()
        result = classify_comments(comments, backend_config=config)

        assert len(result) == 1
        # Falls back to keyword classification
        assert result[0].category == "correctness"
