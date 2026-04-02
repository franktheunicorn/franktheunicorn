"""Tests for moderation flag computation (§2.2)."""

from __future__ import annotations

from franktheunicorn.scoring.moderation import compute_moderation_flags


class TestComputeModerationFlags:
    def test_is_operator_pr(self) -> None:
        assert "is_operator_pr" in compute_moderation_flags({"author": "holdenk"}, "holdenk")
        assert "is_operator_pr" not in compute_moderation_flags({"author": "alice"}, "holdenk")

    def test_draft(self) -> None:
        assert "draft" in compute_moderation_flags({"is_draft": True, "author": "a"}, "op")
        assert "draft" not in compute_moderation_flags({"is_draft": False, "author": "a"}, "op")

    def test_bot(self) -> None:
        assert "bot" in compute_moderation_flags({"author": "dependabot[bot]"}, "op")
        assert "bot" not in compute_moderation_flags({"author": "alice"}, "op")

    def test_large_pr(self) -> None:
        assert "large_pr" in compute_moderation_flags(
            {"author": "a", "additions": 400, "deletions": 200}, "op"
        )
        assert "large_pr" not in compute_moderation_flags(
            {"author": "a", "additions": 10, "deletions": 5}, "op"
        )

    def test_low_context(self) -> None:
        assert "low_context" in compute_moderation_flags(
            {"author": "a", "body": "", "labels": []}, "op"
        )
        assert "low_context" not in compute_moderation_flags(
            {"author": "a", "body": "x" * 60, "labels": []}, "op"
        )
        assert "low_context" not in compute_moderation_flags(
            {"author": "a", "body": "short", "labels": ["bug"]}, "op"
        )

    def test_new_contributor(self) -> None:
        assert "new_contributor" in compute_moderation_flags(
            {"author": "newbie"}, "op", known_authors=["alice"]
        )
        assert "new_contributor" not in compute_moderation_flags(
            {"author": "alice"}, "op", known_authors=["alice"]
        )

    def test_bot_not_new_contributor(self) -> None:
        flags = compute_moderation_flags({"author": "dependabot[bot]"}, "op", known_authors=[])
        assert "new_contributor" not in flags and "bot" in flags

    def test_needs_tests(self) -> None:
        assert "needs_tests" in compute_moderation_flags(
            {"author": "a", "changed_files": ["src/main.py"]}, "op"
        )
        assert "needs_tests" not in compute_moderation_flags(
            {"author": "a", "changed_files": ["src/main.py", "tests/test_main.py"]}, "op"
        )

    def test_likely_unowned(self) -> None:
        pr = {"author": "someone", "pr_age_days": 30, "requested_reviewers": []}
        assert "likely_unowned" in compute_moderation_flags(pr, "op")

    def test_not_unowned_recent(self) -> None:
        pr = {"author": "someone", "pr_age_days": 3, "requested_reviewers": []}
        assert "likely_unowned" not in compute_moderation_flags(pr, "op")

    def test_not_unowned_has_reviewer(self) -> None:
        pr = {"author": "someone", "pr_age_days": 30, "requested_reviewers": ["alice"]}
        assert "likely_unowned" not in compute_moderation_flags(pr, "op")

    def test_not_unowned_draft(self) -> None:
        pr = {
            "author": "someone",
            "pr_age_days": 30,
            "requested_reviewers": [],
            "is_draft": True,
        }
        assert "likely_unowned" not in compute_moderation_flags(pr, "op")

    def test_graceful_missing_keys(self) -> None:
        assert isinstance(compute_moderation_flags({}, "op"), list)
