"""Tests for moderation flag computation."""

from __future__ import annotations

from franktheunicorn.scoring.moderation import compute_moderation_flags


class TestComputeModerationFlags:
    def test_draft(self) -> None:
        assert "draft" in compute_moderation_flags({"is_draft": True, "author": "alice"})
        assert "draft" not in compute_moderation_flags({"is_draft": False, "author": "alice"})

    def test_bot(self) -> None:
        assert "bot" in compute_moderation_flags({"author": "dependabot[bot]"})
        assert "bot" not in compute_moderation_flags({"author": "alice"})

    def test_large_pr(self) -> None:
        assert "large_pr" in compute_moderation_flags(
            {"author": "a", "additions": 400, "deletions": 200}
        )
        assert "large_pr" not in compute_moderation_flags(
            {"author": "a", "additions": 10, "deletions": 5}
        )

    def test_low_context(self) -> None:
        assert "low_context" in compute_moderation_flags({"author": "a", "body": "", "labels": []})
        assert "low_context" in compute_moderation_flags(
            {"author": "a", "body": "fix bug", "labels": []}
        )
        assert "low_context" not in compute_moderation_flags(
            {"author": "a", "body": "short", "labels": ["bug"]}
        )
        assert "low_context" not in compute_moderation_flags(
            {"author": "a", "body": "x" * 60, "labels": []}
        )

    def test_new_contributor(self) -> None:
        assert "new_contributor" in compute_moderation_flags(
            {"author": "newbie"}, known_authors=["alice"]
        )
        assert "new_contributor" not in compute_moderation_flags(
            {"author": "alice"}, known_authors=["alice"]
        )
        # Bots don't get new_contributor
        flags = compute_moderation_flags({"author": "dependabot[bot]"}, known_authors=[])
        assert "new_contributor" not in flags and "bot" in flags
        # None known_authors skips the check
        assert "new_contributor" not in compute_moderation_flags({"author": "newbie"})

    def test_needs_tests(self) -> None:
        assert "needs_tests" in compute_moderation_flags(
            {"author": "a", "changed_files": ["src/main.py"]}
        )
        assert "needs_tests" not in compute_moderation_flags(
            {"author": "a", "changed_files": ["src/main.py", "tests/test_main.py"]}
        )
        assert "needs_tests" not in compute_moderation_flags(
            {"author": "a", "changed_files": ["docs/readme.md"]}
        )

    def test_multiple_flags(self) -> None:
        flags = compute_moderation_flags(
            {
                "author": "dependabot[bot]",
                "is_draft": True,
                "additions": 600,
                "deletions": 0,
                "body": "",
                "labels": [],
            }
        )
        assert set(flags) >= {"draft", "bot", "large_pr", "low_context"}

    def test_graceful_with_missing_keys(self) -> None:
        assert isinstance(compute_moderation_flags({}), list)
