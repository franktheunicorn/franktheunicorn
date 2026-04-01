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
from franktheunicorn.curator.scraper import RawComment


def _make_comment(body: str = "Looks good", **kwargs) -> RawComment:
    defaults = {
        "author": "alice",
        "body": body,
        "diff_context": "@@ -1 +1 @@\n-old\n+new",
        "file_path": "src/main.py",
        "pr_number": 42,
        "pr_title": "Fix bug",
        "created_at": "2026-03-20T10:00:00Z",
        "url": "https://github.com/org/repo/pull/42#r1",
    }
    defaults.update(kwargs)
    return RawComment(**defaults)


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
