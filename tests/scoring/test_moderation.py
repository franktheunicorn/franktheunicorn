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
            {"author": "newbie"},
            "op",
            known_authors=["alice"],
            contributor_evidence_available=True,
        )
        assert "new_contributor" not in compute_moderation_flags(
            {"author": "alice"},
            "op",
            known_authors=["alice"],
            contributor_evidence_available=True,
        )

    def test_absence_without_contributor_evidence_is_not_new(self) -> None:
        assert "new_contributor" not in compute_moderation_flags(
            {"author": "newbie"},
            "op",
            known_authors=["alice"],
            contributor_evidence_available=False,
        )

    def test_bot_not_new_contributor(self) -> None:
        flags = compute_moderation_flags(
            {"author": "dependabot[bot]"},
            "op",
            known_authors=[],
            contributor_evidence_available=True,
        )
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

    def test_wip_title_flag(self) -> None:
        for prefix in ("[WIP]", "WIP:", "Draft:", "[draft]", "[wip] add feature"):
            assert "wip_title" in compute_moderation_flags(
                {"author": "a", "title": prefix}, "op"
            ), f"expected wip_title for title {prefix!r}"

    def test_non_wip_title_no_flag(self) -> None:
        assert "wip_title" not in compute_moderation_flags(
            {"author": "a", "title": "Add amazing feature"}, "op"
        )

    def test_wip_title_not_unowned(self) -> None:
        pr = {
            "author": "someone",
            "title": "WIP: refactor everything",
            "pr_age_days": 30,
            "requested_reviewers": [],
        }
        assert "likely_unowned" not in compute_moderation_flags(pr, "op")

    def test_mentioned_via_requested_reviewer(self) -> None:
        assert "mentioned" in compute_moderation_flags(
            {"author": "alice", "requested_reviewers": ["holdenk"]}, "holdenk"
        )

    def test_mentioned_via_assignee(self) -> None:
        assert "mentioned" in compute_moderation_flags(
            {"author": "alice", "assignees": ["holdenk"]}, "holdenk"
        )

    def test_mentioned_via_body_at_mention(self) -> None:
        assert "mentioned" in compute_moderation_flags(
            {"author": "alice", "body": "hey @holdenk can you take a look?"}, "holdenk"
        )

    def test_mentioned_case_insensitive(self) -> None:
        assert "mentioned" in compute_moderation_flags(
            {"author": "alice", "requested_reviewers": ["HoldenK"]}, "holdenk"
        )

    def test_not_mentioned_when_uninvolved(self) -> None:
        flags = compute_moderation_flags(
            {
                "author": "alice",
                "body": "a routine change to the parser",
                "requested_reviewers": ["bob"],
                "assignees": ["carol"],
            },
            "holdenk",
        )
        assert "mentioned" not in flags

    def test_mentioned_independent_of_operator_pr(self) -> None:
        # Operator both authored AND is a requested reviewer: both flags set,
        # mentioned is not suppressed by is_operator_pr.
        flags = compute_moderation_flags(
            {"author": "holdenk", "requested_reviewers": ["holdenk"]}, "holdenk"
        )
        assert "is_operator_pr" in flags
        assert "mentioned" in flags

    def test_not_mentioned_when_operator_username_empty(self) -> None:
        assert "mentioned" not in compute_moderation_flags(
            {"author": "alice", "requested_reviewers": ["holdenk"]}, ""
        )

    def test_substring_username_does_not_false_fire(self) -> None:
        # "@holden" must not match operator "holdenk" (word-boundary anchored).
        flags = compute_moderation_flags(
            {"author": "alice", "body": "cc @holden not the operator"}, "holdenk"
        )
        assert "mentioned" not in flags

    def test_graceful_missing_keys(self) -> None:
        assert isinstance(compute_moderation_flags({}, "op"), list)


class TestAuthorAssociationDecidesNewContributor:
    """GitHub knows whether an author has committed to the repo; frank's own
    known-authors list is capped at 500 forge contributors plus what it has
    ingested, so on a large repo it was wrong for everyone past the cap."""

    def test_a_contributor_past_the_cap_is_not_new(self) -> None:
        flags = compute_moderation_flags(
            {"author": "returning", "author_association": "CONTRIBUTOR"},
            "op",
            known_authors=["someone-else"],
            contributor_evidence_available=True,
        )
        assert "new_contributor" not in flags

    def test_member_owner_and_collaborator_are_never_new(self) -> None:
        for association in ("MEMBER", "OWNER", "COLLABORATOR"):
            flags = compute_moderation_flags(
                {"author": "x", "author_association": association},
                "op",
                known_authors=[],
                contributor_evidence_available=True,
            )
            assert "new_contributor" not in flags, association

    def test_a_first_timer_is_new_even_without_contributor_evidence(self) -> None:
        for association in ("FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"):
            flags = compute_moderation_flags(
                {"author": "newbie", "author_association": association},
                "op",
                known_authors=None,
                contributor_evidence_available=False,
            )
            assert "new_contributor" in flags, association

    def test_a_first_timer_frank_has_already_seen_is_not_new(self) -> None:
        flags = compute_moderation_flags(
            {"author": "newbie", "author_association": "FIRST_TIME_CONTRIBUTOR"},
            "op",
            known_authors=["newbie"],
            contributor_evidence_available=True,
        )
        assert "new_contributor" not in flags

    def test_no_association_keeps_the_old_rule(self) -> None:
        for association in ("", "NONE"):
            pr = {"author": "stranger", "author_association": association}
            assert "new_contributor" in compute_moderation_flags(
                pr, "op", known_authors=["other"], contributor_evidence_available=True
            )
            assert "new_contributor" not in compute_moderation_flags(
                pr, "op", known_authors=["other"], contributor_evidence_available=False
            )

    def test_a_bot_is_never_new(self) -> None:
        flags = compute_moderation_flags(
            {"author": "renovate[bot]", "author_association": "FIRST_TIME_CONTRIBUTOR"},
            "op",
            known_authors=[],
            contributor_evidence_available=True,
        )
        assert "new_contributor" not in flags
