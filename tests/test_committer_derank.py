"""Tests for committer-is-on-it down-ranking signal (§2.7)."""

from __future__ import annotations

from franktheunicorn.scoring.signals import score_committer_is_on_it


class TestCommitterIsOnIt:
    def test_deranks_when_committer_reviewing(self) -> None:
        reviews = [{"reviewer": "cloud-fan", "author": "alice"}]
        result = score_committer_is_on_it(
            reviews, "holdenk", ["cloud-fan", "dongjoon"], [], []
        )
        assert result == -25

    def test_no_derank_when_operator_is_the_reviewer(self) -> None:
        reviews = [{"reviewer": "holdenk", "author": "alice"}]
        result = score_committer_is_on_it(
            reviews, "holdenk", ["holdenk", "dongjoon"], [], []
        )
        assert result is None

    def test_no_derank_when_no_committer_reviews(self) -> None:
        reviews = [{"reviewer": "random-person", "author": "alice"}]
        result = score_committer_is_on_it(
            reviews, "holdenk", ["cloud-fan"], [], []
        )
        assert result is None

    def test_no_derank_when_pr_in_watch_paths(self) -> None:
        reviews = [{"reviewer": "cloud-fan", "author": "alice"}]
        result = score_committer_is_on_it(
            reviews,
            "holdenk",
            ["cloud-fan"],
            ["sql/catalyst/"],
            ["sql/catalyst/rules/Optimizer.scala"],
        )
        assert result is None

    def test_no_derank_when_operator_mentioned(self) -> None:
        reviews = [{"reviewer": "cloud-fan", "author": "alice"}]
        result = score_committer_is_on_it(
            reviews, "holdenk", ["cloud-fan"], [], [],
            mentioned_or_assigned=True,
        )
        assert result is None

    def test_case_insensitive_committer_match(self) -> None:
        reviews = [{"reviewer": "Cloud-Fan", "author": "alice"}]
        result = score_committer_is_on_it(
            reviews, "holdenk", ["cloud-fan"], [], []
        )
        assert result == -25

    def test_empty_committers(self) -> None:
        reviews = [{"reviewer": "cloud-fan", "author": "alice"}]
        result = score_committer_is_on_it(
            reviews, "holdenk", [], [], []
        )
        assert result is None

    def test_empty_reviews(self) -> None:
        result = score_committer_is_on_it(
            [], "holdenk", ["cloud-fan"], [], []
        )
        assert result is None
