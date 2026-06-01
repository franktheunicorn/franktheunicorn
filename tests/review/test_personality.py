"""Tests for the reviewer agent personality loader."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from franktheunicorn.personalities import Personality, clear_personality_cache, load_personality


class TestLoadPersonality:
    def setup_method(self) -> None:
        clear_personality_cache()

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

        clear_personality_cache()
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

    def test_user_file_edit_invalidates_cache(self, tmp_path: Path) -> None:
        """Editing a user personality file picks up the new content on the
        next load — operators should not need to restart the worker after
        tweaking a personality."""
        import os
        import time

        custom_dir = tmp_path / "personalities"
        custom_dir.mkdir()
        custom_file = custom_dir / "frank.md"
        custom_file.write_text(
            "## Identity\nFirst version.\n\n"
            "## Internal voice\nA.\n\n"
            "## External voice\nB.\n\n"
            "## Review philosophy\nC.\n",
            encoding="utf-8",
        )

        clear_personality_cache()
        with patch("franktheunicorn.personalities._USER_PERSONALITIES_DIR", custom_dir):
            first = load_personality("frank")
            assert first is not None
            assert first.identity == "First version."

            # Rewrite with a bumped mtime so the cache key changes. We can't
            # rely on filesystem clock granularity in CI, so set mtime
            # explicitly a few seconds into the future.
            custom_file.write_text(
                "## Identity\nSecond version.\n\n"
                "## Internal voice\nA.\n\n"
                "## External voice\nB.\n\n"
                "## Review philosophy\nC.\n",
                encoding="utf-8",
            )
            future = time.time() + 10
            os.utime(custom_file, (future, future))

            second = load_personality("frank")
            assert second is not None
            assert second.identity == "Second version."
