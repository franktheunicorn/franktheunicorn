"""Tests for personalities/builder.py."""

from __future__ import annotations

import pytest

from franktheunicorn.curator.classifier import ClassifiedComment
from franktheunicorn.curator.scraper import RawComment
from franktheunicorn.personalities.builder import (
    _compute_stats,
    _format_review_examples,
    _select_examples,
    build_persona_from_comments,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw(body: str, author: str = "holdenk") -> RawComment:
    return RawComment(
        author=author,
        body=body,
        diff_context="@@ hunk",
        file_path="src/main.py",
        pr_number=1,
        pr_title="Test PR",
        created_at="2026-01-01T00:00:00Z",
        url="https://github.com/org/repo/pull/1#r1",
    )


def _make_cc(
    body: str,
    category: str = "correctness",
    tone_flagged: bool = False,
    author: str = "holdenk",
) -> ClassifiedComment:
    return ClassifiedComment(
        raw=_make_raw(body, author),
        category=category,
        tone_flagged=tone_flagged,
        tone_flags=[],
    )


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

LONG_COMMENT = (
    "Use isNullAt(idx) instead of == null — Spark's internal null representation "
    "won't match Java null equality. This will silently return wrong results on "
    "nullable columns."
)
SHORT_COMMENT = "Fix this."
ABRASIVE_COMMENT = "This is obviously wrong, terrible approach."
QUESTION_COMMENT = "Can you explain why this is needed here?"
SUGGESTION_COMMENT = "Consider using a map instead of a loop here for clarity."


@pytest.fixture()
def mixed_comments() -> list[ClassifiedComment]:
    return [
        _make_cc(LONG_COMMENT, "correctness"),
        _make_cc(LONG_COMMENT + " extra context.", "correctness"),
        _make_cc(QUESTION_COMMENT, "correctness"),
        _make_cc(SUGGESTION_COMMENT, "style"),
        _make_cc("You should add a test for the empty case here.", "test-coverage"),
        _make_cc(ABRASIVE_COMMENT, "correctness", tone_flagged=True),
        _make_cc(SHORT_COMMENT, "style"),  # too short for examples
    ]


# ---------------------------------------------------------------------------
# _compute_stats
# ---------------------------------------------------------------------------


class TestComputeStats:
    def test_empty_input_returns_zero_stats(self) -> None:
        stats = _compute_stats("alice", [])
        assert stats.total_comments == 0
        assert stats.category_distribution == {}
        assert stats.top_categories == []

    def test_category_distribution_sums_to_one(self, mixed_comments: list) -> None:
        stats = _compute_stats("holdenk", mixed_comments)
        total_pct = sum(stats.category_distribution.values())
        assert abs(total_pct - 1.0) < 1e-9

    def test_top_categories_ordered_by_frequency(self, mixed_comments: list) -> None:
        stats = _compute_stats("holdenk", mixed_comments)
        # correctness has 4 entries (most), style has 2, test-coverage has 1
        assert stats.top_categories[0] == "correctness"

    def test_tone_flag_rate(self, mixed_comments: list) -> None:
        stats = _compute_stats("holdenk", mixed_comments)
        # 1 out of 7 is tone-flagged
        assert abs(stats.tone_flag_rate - 1 / 7) < 1e-9

    def test_question_rate_detected(self) -> None:
        comments = [_make_cc(QUESTION_COMMENT), _make_cc(LONG_COMMENT)]
        stats = _compute_stats("alice", comments)
        assert stats.question_rate == 0.5

    def test_suggestion_rate_detected(self) -> None:
        comments = [_make_cc(SUGGESTION_COMMENT), _make_cc(LONG_COMMENT)]
        stats = _compute_stats("alice", comments)
        assert stats.suggestion_rate == 0.5

    def test_username_preserved(self) -> None:
        stats = _compute_stats("someuser", [_make_cc("ok")])
        assert stats.username == "someuser"


# ---------------------------------------------------------------------------
# _select_examples
# ---------------------------------------------------------------------------


class TestSelectExamples:
    def test_excludes_tone_flagged(self, mixed_comments: list) -> None:
        examples = _select_examples(mixed_comments)
        for texts in examples.values():
            for t in texts:
                assert "terrible" not in t.lower()

    def test_excludes_short_comments(self, mixed_comments: list) -> None:
        examples = _select_examples(mixed_comments)
        for texts in examples.values():
            for t in texts:
                assert len(t) > 50

    def test_respects_max_per_category(self, mixed_comments: list) -> None:
        examples = _select_examples(mixed_comments, max_per_category=1)
        for texts in examples.values():
            assert len(texts) <= 1

    def test_prefers_longer_comments(self) -> None:
        short_ok = "This is a short but valid comment about things." * 1  # >50
        long_ok = "This is a much longer comment with more context. " * 4
        comments = [_make_cc(short_ok, "correctness"), _make_cc(long_ok, "correctness")]
        examples = _select_examples(comments, max_per_category=1)
        assert examples["correctness"][0] == long_ok.strip()

    def test_empty_input_returns_empty(self) -> None:
        assert _select_examples([]) == {}


# ---------------------------------------------------------------------------
# _format_review_examples
# ---------------------------------------------------------------------------


class TestFormatReviewExamples:
    def test_empty_dict_returns_empty_string(self) -> None:
        assert _format_review_examples({}) == ""

    def test_section_header_present(self) -> None:
        result = _format_review_examples({"correctness": ["Fix the null check."]})
        assert "## Review Examples" in result

    def test_category_subsection_header(self) -> None:
        result = _format_review_examples({"correctness": ["Fix the null check."]})
        assert "### correctness" in result

    def test_blockquote_formatting(self) -> None:
        result = _format_review_examples({"style": ["Use consistent indentation."]})
        assert "> Use consistent indentation." in result

    def test_multi_line_comment_quoted(self) -> None:
        body = "Line one.\nLine two."
        result = _format_review_examples({"correctness": [body]})
        assert "> Line one." in result
        assert "> Line two." in result

    def test_multiple_categories(self) -> None:
        result = _format_review_examples({"correctness": ["Fix null."], "style": ["Fix indent."]})
        assert "### correctness" in result
        assert "### style" in result


# ---------------------------------------------------------------------------
# build_persona_from_comments (integration)
# ---------------------------------------------------------------------------


class TestBuildPersonaFromComments:
    def test_returns_string(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_fallback_includes_identity_section(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments)
        assert "## Identity" in result

    def test_fallback_includes_username(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments)
        assert "holdenk" in result

    def test_fallback_includes_internal_voice(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments)
        assert "## Internal Voice" in result

    def test_fallback_includes_external_voice(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments)
        assert "## External Voice" in result

    def test_fallback_includes_review_philosophy(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments)
        assert "## Review Philosophy" in result

    def test_review_examples_section_present(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments)
        assert "## Review Examples" in result

    def test_verbatim_example_text_in_output(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments)
        # The long clean comment should appear verbatim (as blockquote).
        assert "isNullAt" in result

    def test_tone_flagged_examples_excluded(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments)
        # The abrasive comment must not appear in review examples.
        assert "terrible approach" not in result

    def test_empty_comments_generates_graceful_output(self) -> None:
        result = build_persona_from_comments("nobody", [])
        assert "## Identity" in result
        assert "nobody" in result

    def test_stub_backend_uses_fallback(self, mixed_comments: list) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        stub_cfg = LLMBackendConfig(provider="stub")
        result = build_persona_from_comments("holdenk", mixed_comments, backend_config=stub_cfg)
        assert "## Identity" in result

    def test_no_backend_uses_fallback(self, mixed_comments: list) -> None:
        result = build_persona_from_comments("holdenk", mixed_comments, backend_config=None)
        assert "## Identity" in result
