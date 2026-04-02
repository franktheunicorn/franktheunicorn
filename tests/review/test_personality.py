"""Tests for the reviewer agent personality loader."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from franktheunicorn.personalities import Personality, load_personality


class TestLoadPersonality:
    def setup_method(self) -> None:
        load_personality.cache_clear()

    def test_load_default_frank_personality(self) -> None:
        p = load_personality("frank")
        assert p is not None
        assert p.name == "frank"
        assert "Frank the Unicorn" in p.identity
        assert p.internal_voice
        assert p.external_voice
        assert p.review_philosophy
        assert p.raw

    def test_load_nonexistent_returns_none(self) -> None:
        p = load_personality("nonexistent-unicorn")
        assert p is None

    def test_load_empty_name_returns_none(self) -> None:
        p = load_personality("")
        assert p is None

    def test_personality_sections_parsed_correctly(self) -> None:
        p = load_personality("frank")
        assert p is not None
        # Identity should talk about who Frank is
        assert "unicorn" in p.identity.lower()
        # Internal voice should mention first person / dashboard
        assert "dashboard" in p.internal_voice.lower() or "first person" in p.internal_voice.lower()
        # External voice should mention professional / GitHub
        assert "professional" in p.external_voice.lower() or "github" in p.external_voice.lower()
        # Review philosophy should mention correctness
        assert "correctness" in p.review_philosophy.lower()

    def test_personality_cached(self) -> None:
        p1 = load_personality("frank")
        p2 = load_personality("frank")
        assert p1 is p2

    def test_custom_personality_overrides_bundled(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / "personalities"
        custom_dir.mkdir()
        custom_md = custom_dir / "frank.md"
        custom_md.write_text(
            "# Custom Frank\n\n"
            "## Identity\nI am a custom unicorn.\n\n"
            "## Internal Voice\nCustom internal voice.\n\n"
            "## External Voice\nCustom external voice.\n\n"
            "## Review Philosophy\nCustom philosophy.\n",
            encoding="utf-8",
        )

        load_personality.cache_clear()
        with patch("franktheunicorn.personalities._USER_PERSONALITIES_DIR", custom_dir):
            p = load_personality("frank")

        assert p is not None
        assert p.identity == "I am a custom unicorn."
        assert p.internal_voice == "Custom internal voice."
        assert p.external_voice == "Custom external voice."
        assert p.review_philosophy == "Custom philosophy."

    def test_frozen_dataclass(self) -> None:
        p = load_personality("frank")
        assert p is not None
        assert isinstance(p, Personality)
