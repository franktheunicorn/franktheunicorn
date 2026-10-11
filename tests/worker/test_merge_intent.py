"""Tests for intending-to-merge detection (worker/merge_intent.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from franktheunicorn.worker.merge_intent import (
    EXCERPT_MAX_CHARS,
    INTENT_APPROVED,
    INTENT_CONDITIONAL,
    INTENT_NONE,
    MergeIntent,
    apply_merge_intent,
    classify_body,
    classify_ci,
    count_approvals,
    detect_merge_intent,
)
from tests.factories import PullRequestFactory


def _review(
    login: str,
    state: str,
    body: str = "",
    at: str = "2026-10-01T10:00:00Z",
    commit_id: str = "abc123",
) -> dict[str, Any]:
    return {
        "user": {"login": login},
        "state": state,
        "body": body,
        "submitted_at": at,
        "commit_id": commit_id,
    }


def _comment(login: str, body: str, at: str = "2026-10-01T10:00:00Z") -> dict[str, Any]:
    return {"user": {"login": login}, "body": body, "created_at": at}


class TestClassifyBody:
    @pytest.mark.parametrize(
        "body",
        [
            "LGTM pending CI",
            "Looks good to me once the tests pass.",
            "lgtm, merging once CI is green",
            "I'll merge this when the build passes",
            "Thanks! Will merge after Jenkins is happy.",
            "+1 assuming the checks pass",
            "Once CI passes this looks good to go",
            "Pending tests, LGTM",
            "Looks good, as long as GitHub Actions is green I'll merge.",
        ],
    )
    def test_conditional_phrases(self, body: str) -> None:
        kind, match = classify_body(body)
        assert kind == INTENT_CONDITIONAL
        assert match is not None

    @pytest.mark.parametrize(
        "body",
        [
            "LGTM",
            "lgtm!",
            "Looks good to me.",
            "Looks good",
            "+1",
            "Ship it",
            "Good to go from my side.",
            "I'll merge this later today.",
            "LGTM, one typo in the docstring but not blocking.",
        ],
    )
    def test_plain_approval_phrases(self, body: str) -> None:
        kind, _ = classify_body(body)
        assert kind == INTENT_APPROVED

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "Can you add a test for the empty case?",
            "CI is failing, please take a look.",
            "The tests pass locally for me but not in CI.",
            # LGTM mid-sentence is a quote or a report, not a verdict.
            "Not sure this LGTM, the retry path looks wrong.",
            # Quoted text is somebody else's words.
            "> LGTM pending CI\n\nI disagree, this needs another pass.",
            # Code fences are code.
            "```\nlgtm = True  # pending ci\n```\nWhat does this flag do?",
        ],
    )
    def test_no_verdict(self, body: str) -> None:
        kind, match = classify_body(body)
        assert kind == INTENT_NONE
        assert match is None

    def test_quoted_approval_followed_by_own_verdict(self) -> None:
        kind, _ = classify_body("> Can you review?\n\nLGTM pending CI")
        assert kind == INTENT_CONDITIONAL


class TestDetectMergeIntent:
    def test_no_approvers_means_nothing(self) -> None:
        intent = detect_merge_intent([_review("holdenk", "APPROVED")], [], [])
        assert not intent.is_set

    def test_formal_approval(self) -> None:
        intent = detect_merge_intent(
            [_review("holdenk", "APPROVED", "Thanks!", commit_id="feed")], [], ["holdenk"]
        )
        assert intent.kind == INTENT_APPROVED
        assert intent.source == "review"
        assert intent.by == "holdenk"
        assert intent.commit_sha == "feed"
        assert intent.at == datetime(2026, 10, 1, 10, tzinfo=UTC)

    def test_formal_approval_with_gate_in_body_is_conditional(self) -> None:
        intent = detect_merge_intent(
            [_review("holdenk", "APPROVED", "LGTM pending CI")], [], ["holdenk"]
        )
        assert intent.kind == INTENT_CONDITIONAL
        assert "LGTM pending CI" in intent.excerpt

    def test_conditional_comment(self) -> None:
        intent = detect_merge_intent(
            [], [_comment("holdenk", "Looks good, will merge once CI is green.")], ["holdenk"]
        )
        assert intent.kind == INTENT_CONDITIONAL
        assert intent.source == "comment"
        assert intent.commit_sha == ""

    def test_plain_lgtm_comment_is_approval(self) -> None:
        intent = detect_merge_intent([], [_comment("holdenk", "LGTM")], ["holdenk"])
        assert intent.kind == INTENT_APPROVED

    def test_other_users_are_ignored(self) -> None:
        intent = detect_merge_intent(
            [_review("cloud-fan", "APPROVED")],
            [_comment("dongjoon-hyun", "LGTM pending CI")],
            ["holdenk"],
        )
        assert not intent.is_set

    def test_approver_login_is_case_insensitive(self) -> None:
        intent = detect_merge_intent([_review("HoldenK", "APPROVED")], [], ["holdenk"])
        assert intent.kind == INTENT_APPROVED

    def test_configured_co_approver_counts(self) -> None:
        intent = detect_merge_intent(
            [_review("cloud-fan", "APPROVED")], [], ["holdenk", "cloud-fan"]
        )
        assert intent.kind == INTENT_APPROVED
        assert intent.by == "cloud-fan"

    def test_changes_requested_after_approval_clears(self) -> None:
        intent = detect_merge_intent(
            [
                _review("holdenk", "APPROVED", at="2026-10-01T10:00:00Z"),
                _review("holdenk", "CHANGES_REQUESTED", "Wait, no", at="2026-10-02T10:00:00Z"),
            ],
            [],
            ["holdenk"],
        )
        assert not intent.is_set

    def test_approval_after_changes_requested_sets(self) -> None:
        intent = detect_merge_intent(
            [
                _review("holdenk", "CHANGES_REQUESTED", at="2026-10-01T10:00:00Z"),
                _review("holdenk", "APPROVED", at="2026-10-02T10:00:00Z"),
            ],
            [],
            ["holdenk"],
        )
        assert intent.kind == INTENT_APPROVED

    def test_later_unrelated_comment_keeps_approval(self) -> None:
        intent = detect_merge_intent(
            [_review("holdenk", "APPROVED", at="2026-10-01T10:00:00Z")],
            [_comment("holdenk", "Also, could you update the changelog?", "2026-10-02T10:00:00Z")],
            ["holdenk"],
        )
        assert intent.kind == INTENT_APPROVED

    def test_latest_signal_wins_across_sources(self) -> None:
        """A conditional comment, then the formal approve button: approved."""
        intent = detect_merge_intent(
            [_review("holdenk", "APPROVED", at="2026-10-02T10:00:00Z")],
            [_comment("holdenk", "LGTM pending CI", "2026-10-01T10:00:00Z")],
            ["holdenk"],
        )
        assert intent.kind == INTENT_APPROVED
        assert intent.source == "review"

    def test_undated_signal_sorts_first(self) -> None:
        """A GitLab approval has no timestamp; a dated later comment overrides it."""
        intent = detect_merge_intent(
            [_review("holdenk", "APPROVED", at="")],
            [_comment("holdenk", "LGTM once the pipeline is green", "2026-10-02T10:00:00Z")],
            ["holdenk"],
        )
        assert intent.kind == INTENT_CONDITIONAL

    def test_pending_review_is_ignored(self) -> None:
        intent = detect_merge_intent(
            [_review("holdenk", "PENDING", "LGTM pending CI")], [], ["holdenk"]
        )
        assert not intent.is_set

    def test_commented_review_body_is_classified(self) -> None:
        intent = detect_merge_intent(
            [_review("holdenk", "COMMENTED", "LGTM pending the tests")], [], ["holdenk"]
        )
        assert intent.kind == INTENT_CONDITIONAL

    def test_excerpt_starts_at_the_sentence_with_the_verdict(self) -> None:
        body = "Thanks for the PR. I left two nits inline. LGTM pending CI, I'll merge then."
        intent = detect_merge_intent([], [_comment("holdenk", body)], ["holdenk"])
        assert intent.excerpt.startswith("LGTM pending CI")

    def test_excerpt_is_bounded(self) -> None:
        body = "LGTM pending CI. " + "word " * 200
        intent = detect_merge_intent([], [_comment("holdenk", body)], ["holdenk"])
        assert len(intent.excerpt) <= EXCERPT_MAX_CHARS
        assert intent.excerpt.endswith("…")

    def test_ghost_user_review_does_not_crash(self) -> None:
        intent = detect_merge_intent(
            [{"user": None, "state": "APPROVED", "body": "", "submitted_at": None}],
            [],
            ["holdenk"],
        )
        assert not intent.is_set

    def test_naive_timestamp_is_read_as_utc_and_sorts(self) -> None:
        """A naive stamp next to an aware one must not raise in the sort."""
        intent = detect_merge_intent(
            [_review("holdenk", "APPROVED", at="2026-10-01T10:00:00")],
            [_comment("holdenk", "Hold on, changes needed", "2026-10-02T10:00:00Z")],
            ["holdenk"],
        )
        assert intent.kind == INTENT_APPROVED
        assert intent.at == datetime(2026, 10, 1, 10, tzinfo=UTC)

    def test_unparseable_timestamp_is_undated(self) -> None:
        intent = detect_merge_intent(
            [_review("holdenk", "APPROVED", at="yesterday")], [], ["holdenk"]
        )
        assert intent.kind == INTENT_APPROVED
        assert intent.at is None


class TestCountApprovals:
    def test_latest_verdict_per_reviewer(self) -> None:
        reviews = [
            _review("a", "APPROVED", at="2026-10-01T10:00:00Z"),
            _review("a", "CHANGES_REQUESTED", at="2026-10-02T10:00:00Z"),
            _review("b", "CHANGES_REQUESTED", at="2026-10-01T10:00:00Z"),
            _review("b", "APPROVED", at="2026-10-02T10:00:00Z"),
            _review("c", "APPROVED", at="2026-10-01T10:00:00Z"),
            _review("c", "COMMENTED", at="2026-10-02T10:00:00Z"),
        ]
        assert count_approvals(reviews) == 2

    def test_dismissed_is_not_an_approval(self) -> None:
        assert count_approvals([_review("a", "DISMISSED")]) == 0

    def test_author_is_excluded(self) -> None:
        assert count_approvals([_review("author", "APPROVED")], exclude="Author") == 0

    def test_empty(self) -> None:
        assert count_approvals([]) == 0


@pytest.mark.django_db
class TestApplyMergeIntent:
    def test_sets_fields_and_baselines_commit_from_review(self) -> None:
        pr = PullRequestFactory(head_sha="head1")
        at = datetime(2026, 10, 1, 10, tzinfo=UTC)
        intent = MergeIntent(
            kind=INTENT_APPROVED,
            at=at,
            source="review",
            by="holdenk",
            excerpt="LGTM",
            commit_sha="rev1",
        )
        changed = apply_merge_intent(pr, intent)
        assert pr.merge_intent == INTENT_APPROVED
        assert pr.merge_intent_at == at
        assert pr.merge_intent_by == "holdenk"
        assert pr.merge_intent_commit_sha == "rev1"
        assert set(changed) == {
            "merge_intent",
            "merge_intent_at",
            "merge_intent_source",
            "merge_intent_by",
            "merge_intent_excerpt",
            "merge_intent_commit_sha",
        }

    def test_comment_baselines_on_current_head(self) -> None:
        pr = PullRequestFactory(head_sha="head1")
        intent = MergeIntent(kind=INTENT_CONDITIONAL, source="comment", by="holdenk")
        apply_merge_intent(pr, intent)
        assert pr.merge_intent_commit_sha == "head1"
        assert pr.moved_since_approval is False

    def test_push_after_approval_does_not_rebaseline(self) -> None:
        at = datetime(2026, 10, 1, 10, tzinfo=UTC)
        pr = PullRequestFactory(head_sha="head1")
        intent = MergeIntent(kind=INTENT_CONDITIONAL, at=at, source="comment", by="holdenk")
        apply_merge_intent(pr, intent)
        pr.head_sha = "head2"
        changed = apply_merge_intent(pr, intent)
        assert changed == []
        assert pr.merge_intent_commit_sha == "head1"
        assert pr.moved_since_approval is True

    def test_new_approval_rebaselines(self) -> None:
        pr = PullRequestFactory(head_sha="head1")
        first = MergeIntent(
            kind=INTENT_APPROVED, at=datetime(2026, 10, 1, tzinfo=UTC), source="comment", by="h"
        )
        apply_merge_intent(pr, first)
        pr.head_sha = "head2"
        second = MergeIntent(
            kind=INTENT_APPROVED, at=datetime(2026, 10, 3, tzinfo=UTC), source="comment", by="h"
        )
        apply_merge_intent(pr, second)
        assert pr.merge_intent_commit_sha == "head2"
        assert pr.moved_since_approval is False

    def test_clearing_resets_everything(self) -> None:
        pr = PullRequestFactory(
            head_sha="head1",
            merge_intent=INTENT_APPROVED,
            merge_intent_by="holdenk",
            merge_intent_excerpt="LGTM",
            merge_intent_commit_sha="head0",
        )
        changed = apply_merge_intent(pr, MergeIntent())
        assert pr.merge_intent == ""
        assert pr.merge_intent_by == ""
        assert pr.merge_intent_excerpt == ""
        assert pr.merge_intent_commit_sha == ""
        assert "merge_intent" in changed

    def test_moved_since_approval_needs_both_shas(self) -> None:
        pr = PullRequestFactory.build(head_sha="", merge_intent=INTENT_APPROVED)
        pr.merge_intent_commit_sha = "x"
        assert pr.moved_since_approval is False


class TestClassifyCI:
    def test_no_checks_is_unknown(self) -> None:
        assert classify_ci([], []) == ""

    def test_all_green(self) -> None:
        assert classify_ci([{"conclusion": "success"}], [{"state": "success"}]) == "pass"

    def test_neutral_and_skipped_are_fine(self) -> None:
        assert classify_ci([{"conclusion": "neutral"}, {"conclusion": "skipped"}], []) == "pass"

    def test_any_failure_wins(self) -> None:
        runs = [{"conclusion": "success"}, {"conclusion": None}, {"conclusion": "failure"}]
        assert classify_ci(runs, []) == "fail"

    def test_status_error_is_failure(self) -> None:
        assert classify_ci([{"conclusion": "success"}], [{"state": "error"}]) == "fail"

    def test_running_is_pending(self) -> None:
        assert classify_ci([{"conclusion": "success"}, {"conclusion": None}], []) == "pending"
        assert classify_ci([], [{"state": "pending"}]) == "pending"
