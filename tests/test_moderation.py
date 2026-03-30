"""Tests for moderation flag computation."""

from __future__ import annotations

from franktheunicorn.scoring.moderation import compute_moderation_flags


class TestComputeModerationFlags:
    def test_draft(self) -> None:
        flags = compute_moderation_flags({"is_draft": True, "author": "alice"})
        assert "draft" in flags

    def test_not_draft(self) -> None:
        flags = compute_moderation_flags({"is_draft": False, "author": "alice"})
        assert "draft" not in flags

    def test_bot(self) -> None:
        flags = compute_moderation_flags({"author": "dependabot[bot]"})
        assert "bot" in flags

    def test_human_not_bot(self) -> None:
        flags = compute_moderation_flags({"author": "alice"})
        assert "bot" not in flags

    def test_large_pr(self) -> None:
        flags = compute_moderation_flags({"author": "alice", "additions": 400, "deletions": 200})
        assert "large_pr" in flags

    def test_small_pr(self) -> None:
        flags = compute_moderation_flags({"author": "alice", "additions": 10, "deletions": 5})
        assert "large_pr" not in flags

    def test_low_context_empty_body_no_labels(self) -> None:
        flags = compute_moderation_flags({"author": "alice", "body": "", "labels": []})
        assert "low_context" in flags

    def test_low_context_short_body_no_labels(self) -> None:
        flags = compute_moderation_flags({"author": "alice", "body": "fix bug", "labels": []})
        assert "low_context" in flags

    def test_not_low_context_with_labels(self) -> None:
        flags = compute_moderation_flags({"author": "alice", "body": "short", "labels": ["bug"]})
        assert "low_context" not in flags

    def test_not_low_context_long_body(self) -> None:
        flags = compute_moderation_flags({"author": "alice", "body": "x" * 60, "labels": []})
        assert "low_context" not in flags

    def test_new_contributor(self) -> None:
        flags = compute_moderation_flags(
            {"author": "newbie"},
            known_authors=["alice", "bob"],
        )
        assert "new_contributor" in flags

    def test_known_author_not_new(self) -> None:
        flags = compute_moderation_flags(
            {"author": "alice"},
            known_authors=["alice", "bob"],
        )
        assert "new_contributor" not in flags

    def test_bot_not_new_contributor(self) -> None:
        flags = compute_moderation_flags(
            {"author": "dependabot[bot]"},
            known_authors=[],
        )
        assert "new_contributor" not in flags
        assert "bot" in flags

    def test_no_known_authors_skips_new_contributor(self) -> None:
        """When known_authors is None, new_contributor flag is not computed."""
        flags = compute_moderation_flags({"author": "newbie"})
        assert "new_contributor" not in flags

    def test_needs_tests_source_without_tests(self) -> None:
        flags = compute_moderation_flags(
            {"author": "alice", "changed_files": ["src/main.py", "src/utils.py"]}
        )
        assert "needs_tests" in flags

    def test_needs_tests_source_with_tests(self) -> None:
        flags = compute_moderation_flags(
            {"author": "alice", "changed_files": ["src/main.py", "tests/test_main.py"]}
        )
        assert "needs_tests" not in flags

    def test_needs_tests_no_source(self) -> None:
        flags = compute_moderation_flags({"author": "alice", "changed_files": ["docs/readme.md"]})
        assert "needs_tests" not in flags

    def test_multiple_flags(self) -> None:
        flags = compute_moderation_flags(
            {
                "author": "dependabot[bot]",
                "is_draft": True,
                "additions": 600,
                "deletions": 0,
                "body": "",
                "labels": [],
                "changed_files": [],
            },
        )
        assert "draft" in flags
        assert "bot" in flags
        assert "large_pr" in flags
        assert "low_context" in flags

    def test_graceful_with_missing_keys(self) -> None:
        """Missing keys should not raise — degrade gracefully."""
        flags = compute_moderation_flags({})
        assert isinstance(flags, list)
