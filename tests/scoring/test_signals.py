"""Tests for pure scoring signal functions (§2.1)."""

from __future__ import annotations

from franktheunicorn.scoring.signals import (
    WEIGHTS,
    is_ai_agent,
    is_likely_bot,
    path_overlap_fraction,
    score_ai_generated,
    score_cve_file_history,
    score_has_review_request,
    score_keyword_match,
    score_llm_interest,
    score_mentioned_or_assigned,
    score_new_human_contributor,
    score_path_overlap,
    score_pending_response,
    score_prior_review_history,
    score_updated_since_operator_review,
)


class TestIsLikelyBot:
    def test_bots(self) -> None:
        for name in ("dependabot[bot]", "dependabot", "renovate", "greenkeeper", "RENOVATE"):
            assert is_likely_bot(name) is True

    def test_human(self) -> None:
        assert is_likely_bot("alice-dev") is False


class TestIsAiAgent:
    def test_bot_patterns(self) -> None:
        assert is_ai_agent("dependabot[bot]", []) is True

    def test_configured_agent(self) -> None:
        assert is_ai_agent("copilot-workspace", ["copilot-workspace"]) is True

    def test_case_insensitive(self) -> None:
        assert is_ai_agent("CodexBot", ["codexbot"]) is True

    def test_human(self) -> None:
        assert is_ai_agent("alice", ["copilot-workspace"]) is False


class TestPathOverlap:
    def test_empty(self) -> None:
        assert path_overlap_fraction([], ["src/"]) == 0.0

    def test_partial(self) -> None:
        assert (
            path_overlap_fraction(["sql/catalyst/a.scala", "core/b.scala"], ["sql/catalyst/"])
            == 0.5
        )

    def test_full(self) -> None:
        assert path_overlap_fraction(["src/a.py", "src/b.py"], ["src/"]) == 1.0

    def test_signal(self) -> None:
        result = score_path_overlap(["src/a.py", "docs/b.md"], ["src/"])
        assert result == round(WEIGHTS["path_overlap"] * 0.5)

    def test_signal_none(self) -> None:
        assert score_path_overlap([], ["src/"]) is None
        assert score_path_overlap(["a.py"], []) is None


class TestMentionedOrAssigned:
    def test_assignee(self) -> None:
        assert (
            score_mentioned_or_assigned("", ["holdenk"], "holdenk")
            == WEIGHTS["mentioned_or_assigned"]
        )

    def test_mention_in_body(self) -> None:
        assert (
            score_mentioned_or_assigned("cc @holdenk for review", [], "holdenk")
            == WEIGHTS["mentioned_or_assigned"]
        )

    def test_no_match(self) -> None:
        assert score_mentioned_or_assigned("some body", ["alice"], "holdenk") is None


class TestHasReviewRequest:
    def test_requested(self) -> None:
        assert score_has_review_request(["holdenk"], "holdenk") == WEIGHTS["has_review_request"]

    def test_not_requested(self) -> None:
        assert score_has_review_request(["alice"], "holdenk") is None

    def test_empty(self) -> None:
        assert score_has_review_request([], "holdenk") is None

    def test_case_insensitive(self) -> None:
        assert score_has_review_request(["HoldenK"], "holdenk") == WEIGHTS["has_review_request"]


class TestPriorReviewHistory:
    def test_reviewed(self) -> None:
        history = [{"author": "alice", "reviewer": "holdenk"}]
        assert (
            score_prior_review_history("alice", "holdenk", history)
            == WEIGHTS["prior_review_history"]
        )

    def test_not_reviewed(self) -> None:
        history = [{"author": "bob", "reviewer": "holdenk"}]
        assert score_prior_review_history("alice", "holdenk", history) is None

    def test_reverse_doesnt_count(self) -> None:
        history = [{"author": "holdenk", "reviewer": "alice"}]
        assert score_prior_review_history("alice", "holdenk", history) is None

    def test_empty(self) -> None:
        assert score_prior_review_history("alice", "holdenk", []) is None


class TestNewHumanContributor:
    def test_new(self) -> None:
        assert (
            score_new_human_contributor("newbie", "holdenk", []) == WEIGHTS["new_human_contributor"]
        )

    def test_known(self) -> None:
        assert score_new_human_contributor("known", "holdenk", ["known"]) is None

    def test_operator(self) -> None:
        assert score_new_human_contributor("holdenk", "holdenk", []) is None

    def test_bot(self) -> None:
        assert score_new_human_contributor("dependabot[bot]", "holdenk", []) is None

    def test_ai_agent(self) -> None:
        assert (
            score_new_human_contributor("codex-bot", "holdenk", [], ai_agents=["codex-bot"]) is None
        )


class TestKeywordMatch:
    def test_match_title(self) -> None:
        assert score_keyword_match("Fix OOM in executor", "", ["OOM"]) == WEIGHTS["keyword_match"]

    def test_match_body(self) -> None:
        assert (
            score_keyword_match("PR", "fixes memory leak", ["memory"]) == WEIGHTS["keyword_match"]
        )

    def test_case_insensitive(self) -> None:
        assert (
            score_keyword_match("RLIMIT_AS change", "", ["rlimit_as"]) == WEIGHTS["keyword_match"]
        )

    def test_no_match(self) -> None:
        assert score_keyword_match("Add tests", "test body", ["memory"]) is None

    def test_no_keywords(self) -> None:
        assert score_keyword_match("anything", "anything", []) is None


class TestAiGenerated:
    def test_bot(self) -> None:
        result = score_ai_generated("dependabot[bot]")
        assert result == WEIGHTS["ai_generated"]
        assert result is not None and result > 0

    def test_configured_agent(self) -> None:
        assert score_ai_generated("codex-bot", ai_agents=["codex-bot"]) == WEIGHTS["ai_generated"]

    def test_human(self) -> None:
        assert score_ai_generated("alice-dev") is None


class TestLlmInterest:
    def test_high(self) -> None:
        assert score_llm_interest("high") == WEIGHTS["llm_interest"]

    def test_medium(self) -> None:
        assert score_llm_interest("medium") == WEIGHTS["llm_interest"] // 2

    def test_low(self) -> None:
        assert score_llm_interest("low") is None

    def test_none(self) -> None:
        assert score_llm_interest(None) is None

    def test_case_insensitive(self) -> None:
        assert score_llm_interest("HIGH") == WEIGHTS["llm_interest"]
        assert score_llm_interest(" Medium ") == WEIGHTS["llm_interest"] // 2


class TestRecentlyUpdated:
    def test_updated_today(self) -> None:
        from franktheunicorn.scoring.signals import WEIGHTS, score_recently_updated

        assert score_recently_updated(2.0) == WEIGHTS["recently_updated"]

    def test_updated_this_week(self) -> None:
        from franktheunicorn.scoring.signals import WEIGHTS, score_recently_updated

        result = score_recently_updated(72.0)
        assert result == WEIGHTS["recently_updated"] // 2

    def test_updated_long_ago(self) -> None:
        from franktheunicorn.scoring.signals import score_recently_updated

        assert score_recently_updated(200.0) is None

    def test_boundary_24h(self) -> None:
        from franktheunicorn.scoring.signals import WEIGHTS, score_recently_updated

        # Exactly 24h should get the week boost, not the today boost
        assert score_recently_updated(24.0) == WEIGHTS["recently_updated"] // 2

    def test_boundary_168h(self) -> None:
        from franktheunicorn.scoring.signals import score_recently_updated

        # Exactly 168h (7 days) should return None
        assert score_recently_updated(168.0) is None

    def test_none_input(self) -> None:
        from franktheunicorn.scoring.signals import score_recently_updated

        assert score_recently_updated(None) is None

    def test_zero_hours(self) -> None:
        from franktheunicorn.scoring.signals import WEIGHTS, score_recently_updated

        assert score_recently_updated(0.0) == WEIGHTS["recently_updated"]


class TestMergeConflict:
    def test_mergeable_true(self) -> None:
        from franktheunicorn.scoring.signals import score_merge_conflict

        assert score_merge_conflict(True) is None

    def test_mergeable_false(self) -> None:
        from franktheunicorn.scoring.signals import WEIGHTS, score_merge_conflict

        assert score_merge_conflict(False) == WEIGHTS["merge_conflict"]

    def test_mergeable_none(self) -> None:
        from franktheunicorn.scoring.signals import score_merge_conflict

        assert score_merge_conflict(None) is None


class TestUpdatedSinceOperatorReview:
    def test_no_operator_review(self) -> None:
        assert score_updated_since_operator_review(None, "2026-03-30T12:00:00Z") is None

    def test_no_pr_updated_at(self) -> None:
        assert score_updated_since_operator_review("2026-03-29T12:00:00Z", None) is None

    def test_both_none(self) -> None:
        assert score_updated_since_operator_review(None, None) is None

    def test_not_updated_since(self) -> None:
        assert (
            score_updated_since_operator_review("2026-03-30T12:00:00Z", "2026-03-29T12:00:00Z")
            is None
        )

    def test_same_timestamp(self) -> None:
        assert (
            score_updated_since_operator_review("2026-03-30T12:00:00Z", "2026-03-30T12:00:00Z")
            is None
        )

    def test_within_grace_period(self) -> None:
        """Update within 5 minutes of review is ignored (operator's own comment)."""
        assert (
            score_updated_since_operator_review("2026-03-30T12:00:00Z", "2026-03-30T12:03:00Z")
            is None
        )

    def test_updated_since(self) -> None:
        result = score_updated_since_operator_review("2026-03-29T12:00:00Z", "2026-03-30T12:00:00Z")
        assert result == WEIGHTS["updated_since_operator_review"]

    def test_updated_just_past_grace_period(self) -> None:
        """Update 6 minutes after review fires the signal."""
        result = score_updated_since_operator_review("2026-03-30T12:00:00Z", "2026-03-30T12:06:00Z")
        assert result == WEIGHTS["updated_since_operator_review"]

    def test_invalid_timestamp(self) -> None:
        assert score_updated_since_operator_review("not-a-date", "2026-03-30T12:00:00Z") is None


class TestPendingResponse:
    def test_no_operator_review(self) -> None:
        assert score_pending_response(None, ["2026-03-30T12:00:00Z"]) is None

    def test_no_replies(self) -> None:
        assert score_pending_response("2026-03-29T12:00:00Z", []) is None

    def test_empty_posted_at(self) -> None:
        assert score_pending_response("", ["2026-03-30T12:00:00Z"]) is None

    def test_has_reply(self) -> None:
        result = score_pending_response("2026-03-29T12:00:00Z", ["2026-03-30T12:00:00Z"])
        assert result == WEIGHTS["pending_response"]

    def test_multiple_replies(self) -> None:
        result = score_pending_response(
            "2026-03-29T12:00:00Z",
            ["2026-03-30T10:00:00Z", "2026-03-30T14:00:00Z"],
        )
        assert result == WEIGHTS["pending_response"]


class TestCveFileHistory:
    def test_no_cve_files(self) -> None:
        assert score_cve_file_history(["src/main.py"], []) is None

    def test_no_changed_files(self) -> None:
        assert score_cve_file_history([], ["src/auth.py"]) is None

    def test_both_empty(self) -> None:
        assert score_cve_file_history([], []) is None

    def test_full_overlap(self) -> None:
        result = score_cve_file_history(
            ["src/auth.py", "src/crypto.py"],
            ["src/auth.py", "src/crypto.py"],
        )
        assert result == WEIGHTS["cve_file_history"]

    def test_partial_overlap(self) -> None:
        result = score_cve_file_history(
            ["src/auth.py", "src/views.py", "src/models.py", "src/utils.py"],
            ["src/auth.py"],
        )
        # 1/4 overlap = round(25 * 0.25) = 6
        assert result == round(WEIGHTS["cve_file_history"] * 0.25)

    def test_no_overlap(self) -> None:
        assert score_cve_file_history(["src/views.py"], ["src/auth.py"]) is None

    def test_glob_pattern_matching(self) -> None:
        result = score_cve_file_history(
            ["sql/catalyst/optimizer/Rules.scala"],
            ["sql/catalyst/optimizer/**"],
        )
        assert result == WEIGHTS["cve_file_history"]

    def test_prefix_pattern_matching(self) -> None:
        result = score_cve_file_history(
            ["src/auth/login.py"],
            ["src/auth/"],
        )
        assert result == WEIGHTS["cve_file_history"]

    def test_exact_match(self) -> None:
        result = score_cve_file_history(
            ["src/auth.py"],
            ["src/auth.py"],
        )
        assert result == WEIGHTS["cve_file_history"]
