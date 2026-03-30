"""Tests for pure scoring signal functions."""

from __future__ import annotations

from franktheunicorn.scoring.signals import (
    WEIGHTS,
    is_likely_bot,
    path_overlap_fraction,
    score_ai_generated,
    score_frequent_contributor,
    score_large_pr,
    score_new_contributor,
    score_operator_is_author,
    score_path_overlap,
    score_review_requested,
)


class TestIsLikelyBot:
    def test_bot_suffix(self) -> None:
        assert is_likely_bot("dependabot[bot]") is True

    def test_dependabot(self) -> None:
        assert is_likely_bot("dependabot") is True

    def test_renovate(self) -> None:
        assert is_likely_bot("renovate") is True

    def test_greenkeeper(self) -> None:
        assert is_likely_bot("greenkeeper") is True

    def test_human(self) -> None:
        assert is_likely_bot("alice-dev") is False

    def test_case_insensitive(self) -> None:
        assert is_likely_bot("Dependabot[bot]") is True
        assert is_likely_bot("RENOVATE") is True


class TestPathOverlapFraction:
    def test_empty_files(self) -> None:
        assert path_overlap_fraction([], ["src/"]) == 0.0

    def test_no_overlap(self) -> None:
        assert path_overlap_fraction(["docs/readme.md"], ["src/"]) == 0.0

    def test_partial_overlap(self) -> None:
        assert (
            path_overlap_fraction(
                ["sql/catalyst/a.scala", "core/b.scala"],
                ["sql/catalyst/"],
            )
            == 0.5
        )

    def test_full_overlap(self) -> None:
        assert (
            path_overlap_fraction(
                ["src/a.py", "src/b.py"],
                ["src/"],
            )
            == 1.0
        )

    def test_multiple_watched_paths(self) -> None:
        result = path_overlap_fraction(
            ["src/a.py", "lib/b.py", "docs/c.md"],
            ["src/", "lib/"],
        )
        assert abs(result - 2 / 3) < 1e-9


class TestScoreOperatorIsAuthor:
    def test_match(self) -> None:
        assert score_operator_is_author("holdenk", "holdenk") == WEIGHTS["operator_is_author"]

    def test_case_insensitive(self) -> None:
        assert score_operator_is_author("HoldenK", "holdenk") == WEIGHTS["operator_is_author"]

    def test_no_match(self) -> None:
        assert score_operator_is_author("someone", "holdenk") is None


class TestScoreReviewRequested:
    def test_requested(self) -> None:
        result = score_review_requested(["holdenk", "other"], "holdenk")
        assert result == WEIGHTS["review_requested"]

    def test_not_requested(self) -> None:
        assert score_review_requested(["other"], "holdenk") is None

    def test_empty_reviewers(self) -> None:
        assert score_review_requested([], "holdenk") is None

    def test_case_insensitive(self) -> None:
        result = score_review_requested(["HoldenK"], "holdenk")
        assert result == WEIGHTS["review_requested"]


class TestScorePathOverlap:
    def test_overlap(self) -> None:
        result = score_path_overlap(
            ["sql/catalyst/a.scala", "core/b.scala"],
            ["sql/catalyst/"],
        )
        assert result is not None
        assert result == round(WEIGHTS["path_overlap"] * 0.5, 4)

    def test_no_watched_paths(self) -> None:
        assert score_path_overlap(["a.py"], []) is None

    def test_no_changed_files(self) -> None:
        assert score_path_overlap([], ["src/"]) is None

    def test_no_overlap(self) -> None:
        assert score_path_overlap(["docs/a.md"], ["src/"]) is None


class TestScoreFrequentContributor:
    def test_known(self) -> None:
        result = score_frequent_contributor("cloud-fan", ["cloud-fan", "dongjoon-hyun"])
        assert result == WEIGHTS["frequent_contributor"]

    def test_unknown(self) -> None:
        assert score_frequent_contributor("stranger", ["cloud-fan"]) is None

    def test_case_insensitive(self) -> None:
        result = score_frequent_contributor("Cloud-Fan", ["cloud-fan"])
        assert result == WEIGHTS["frequent_contributor"]


class TestScoreNewContributor:
    def test_new(self) -> None:
        result = score_new_contributor("brand-new", "holdenk", ["cloud-fan"], [])
        assert result == WEIGHTS["new_contributor"]

    def test_known_author(self) -> None:
        result = score_new_contributor("returning", "holdenk", [], ["returning"])
        assert result is None

    def test_frequent_contributor_excluded(self) -> None:
        result = score_new_contributor("cloud-fan", "holdenk", ["cloud-fan"], [])
        assert result is None

    def test_operator_excluded(self) -> None:
        result = score_new_contributor("holdenk", "holdenk", [], [])
        assert result is None

    def test_bot_excluded(self) -> None:
        result = score_new_contributor("dependabot[bot]", "holdenk", [], [])
        assert result is None


class TestScoreAiGenerated:
    def test_bot(self) -> None:
        result = score_ai_generated("dependabot[bot]")
        assert result == WEIGHTS["ai_generated_penalty"]
        assert result < 0

    def test_human(self) -> None:
        assert score_ai_generated("alice-dev") is None


class TestScoreLargePr:
    def test_large(self) -> None:
        result = score_large_pr(400, 200)
        assert result == WEIGHTS["large_pr_penalty"]
        assert result < 0

    def test_small(self) -> None:
        assert score_large_pr(100, 50) is None

    def test_at_threshold(self) -> None:
        assert score_large_pr(250, 250) is None

    def test_custom_threshold(self) -> None:
        result = score_large_pr(50, 60, threshold=100)
        assert result == WEIGHTS["large_pr_penalty"]
