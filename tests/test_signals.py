"""Tests for pure scoring signal functions (§2.1)."""

from __future__ import annotations

from franktheunicorn.scoring.signals import (
    WEIGHTS,
    is_ai_agent,
    is_likely_bot,
    path_overlap_fraction,
    score_ai_generated,
    score_has_review_request,
    score_keyword_match,
    score_llm_interest,
    score_mentioned_or_assigned,
    score_new_human_contributor,
    score_path_overlap,
    score_prior_review_history,
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
        assert result is not None and result < 0

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
