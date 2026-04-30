"""Tests for personalities/__init__.py — loader, examples parsing, refresh."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from franktheunicorn.personalities import (
    _parse_review_examples,
    load_personality,
    refresh_personality,
)


class TestParseReviewExamples:
    """Unit tests for the ``## Review Examples`` section parser."""

    def test_empty_string_returns_empty_tuple(self) -> None:
        assert _parse_review_examples("") == ()

    def test_single_category_single_example(self) -> None:
        body = "### correctness\n> Fix the null check.\n"
        result = _parse_review_examples(body)
        assert len(result) == 1
        assert result[0][0] == "correctness"
        assert result[0][1] == "Fix the null check."

    def test_multiple_categories(self) -> None:
        body = "### correctness\n> Fix null.\n\n### style\n> Use consistent indentation.\n"
        result = _parse_review_examples(body)
        assert len(result) == 2
        categories = [r[0] for r in result]
        assert "correctness" in categories
        assert "style" in categories

    def test_multi_line_blockquote(self) -> None:
        body = "### correctness\n> Line one.\n> Line two.\n"
        result = _parse_review_examples(body)
        assert result[0][1] == "Line one.\nLine two."

    def test_empty_blockquote_line_preserved(self) -> None:
        body = "### correctness\n> Para one.\n>\n> Para two.\n"
        result = _parse_review_examples(body)
        text = result[0][1]
        assert "Para one." in text
        assert "Para two." in text

    def test_no_subsections_returns_empty(self) -> None:
        body = "Some text without subsection headers.\n"
        result = _parse_review_examples(body)
        assert result == ()

    def test_subsection_without_blockquote_excluded(self) -> None:
        body = "### correctness\nJust prose, no blockquote.\n"
        result = _parse_review_examples(body)
        assert result == ()

    def test_category_lowercased(self) -> None:
        body = "### Correctness\n> Fix it.\n"
        result = _parse_review_examples(body)
        assert result[0][0] == "correctness"


class TestLoadPersonalityExamples:
    """Tests for Personality.examples populated from ## Review Examples."""

    def setup_method(self) -> None:
        load_personality.cache_clear()

    def test_frank_personality_has_no_examples(self) -> None:
        """The bundled frank.md has no Review Examples section — empty tuple."""
        p = load_personality("frank")
        assert p is not None
        assert p.examples == ()

    def test_custom_personality_with_examples(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / "personalities"
        custom_dir.mkdir()
        md = custom_dir / "reviewer.md"
        md.write_text(
            "# Reviewer\n\n"
            "## Identity\nYou are a reviewer.\n\n"
            "## Internal Voice\nDirect.\n\n"
            "## External Voice\nProfessional.\n\n"
            "## Review Philosophy\n- Correctness first.\n\n"
            "## Review Examples\n\n"
            "### correctness\n"
            "> Use isNullAt(idx) instead of == null.\n\n"
            "### test-coverage\n"
            "> Please add a test for the empty case.\n",
            encoding="utf-8",
        )
        load_personality.cache_clear()
        with patch("franktheunicorn.personalities._USER_PERSONALITIES_DIR", custom_dir):
            p = load_personality("reviewer")

        assert p is not None
        assert len(p.examples) == 2
        cats = [e[0] for e in p.examples]
        assert "correctness" in cats
        assert "test-coverage" in cats

    def test_examples_content_verbatim(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / "personalities"
        custom_dir.mkdir()
        md = custom_dir / "reviewer.md"
        md.write_text(
            "# R\n\n"
            "## Identity\nYou.\n\n"
            "## Internal Voice\nX.\n\n"
            "## External Voice\nY.\n\n"
            "## Review Philosophy\n- Z.\n\n"
            "## Review Examples\n\n"
            "### correctness\n"
            "> Fix the null check here.\n",
            encoding="utf-8",
        )
        load_personality.cache_clear()
        with patch("franktheunicorn.personalities._USER_PERSONALITIES_DIR", custom_dir):
            p = load_personality("reviewer")

        assert p is not None
        texts = [e[1] for e in p.examples]
        assert any("Fix the null check here." in t for t in texts)

    def test_personality_frozen_dataclass_with_examples(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / "personalities"
        custom_dir.mkdir()
        md = custom_dir / "rev.md"
        md.write_text(
            "# R\n\n"
            "## Identity\nYou.\n\n"
            "## Internal Voice\nX.\n\n"
            "## External Voice\nY.\n\n"
            "## Review Philosophy\n- Z.\n\n"
            "## Review Examples\n\n"
            "### style\n> Use 4 spaces.\n",
            encoding="utf-8",
        )
        load_personality.cache_clear()
        with patch("franktheunicorn.personalities._USER_PERSONALITIES_DIR", custom_dir):
            p = load_personality("rev")

        assert p is not None
        with pytest.raises(AttributeError):
            p.examples = ()  # type: ignore[misc]  # frozen dataclass


class TestRefreshPersonality:
    """Tests for refresh_personality()."""

    def setup_method(self) -> None:
        load_personality.cache_clear()

    def test_refresh_clears_cache(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / "personalities"
        custom_dir.mkdir()
        md = custom_dir / "frank.md"
        md.write_text(
            "# Frank\n\n"
            "## Identity\nVersion 1.\n\n"
            "## Internal Voice\nV1.\n\n"
            "## External Voice\nV1.\n\n"
            "## Review Philosophy\n- V1.\n",
            encoding="utf-8",
        )

        load_personality.cache_clear()
        with patch("franktheunicorn.personalities._USER_PERSONALITIES_DIR", custom_dir):
            p1 = load_personality("frank")
            assert p1 is not None
            assert "Version 1" in p1.identity

            # Overwrite the file.
            md.write_text(
                "# Frank\n\n"
                "## Identity\nVersion 2.\n\n"
                "## Internal Voice\nV2.\n\n"
                "## External Voice\nV2.\n\n"
                "## Review Philosophy\n- V2.\n",
                encoding="utf-8",
            )

            # Without refresh, cache returns the old version.
            p_cached = load_personality("frank")
            assert p_cached is p1  # same object from cache

            # After refresh, the new version is loaded.
            refresh_personality("frank")
            p2 = load_personality("frank")
            assert p2 is not None
            assert "Version 2" in p2.identity

    def test_refresh_unknown_name_is_safe(self) -> None:
        # Should not raise even for a name that was never loaded.
        refresh_personality("nonexistent-unicorn-xyz")
