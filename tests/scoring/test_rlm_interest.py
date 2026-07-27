"""Tests for the optional RLM interest judge."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import LLMBackendConfig, ProjectConfig, RLMScoringConfig
from franktheunicorn.scoring.rlm_interest import judge_interest
from franktheunicorn.scoring.scorer import score_pull_request_from_model


def _config(*, interest_enabled: bool) -> ProjectConfig:
    return ProjectConfig(
        owner="apache",
        repo="spark",
        rlm_scoring=RLMScoringConfig(
            interest_enabled=interest_enabled,
            leaf=LLMBackendConfig(provider="stub"),
        ),
    )


@pytest.mark.django_db
def test_judge_interest_disabled_returns_none(db_pr) -> None:
    assert judge_interest(db_pr, _config(interest_enabled=False)) is None


@pytest.mark.django_db
def test_judge_interest_enabled_returns_label(db_pr) -> None:
    # db_pr has two changed files → stub produces findings → a label.
    label = judge_interest(db_pr, _config(interest_enabled=True))
    assert label in {"high", "medium"}


@pytest.mark.django_db
def test_judge_interest_no_changed_files_returns_none(db_pr, make_pr) -> None:
    pr = make_pr(changed_files=[])
    assert judge_interest(pr, _config(interest_enabled=True)) is None


@pytest.mark.django_db
def test_scorer_populates_llm_interest_when_enabled(db_pr) -> None:
    _, breakdown = score_pull_request_from_model(db_pr, _config(interest_enabled=True), "holdenk")
    assert "llm_interest" in breakdown


@pytest.mark.django_db
def test_scorer_skips_llm_interest_when_disabled(db_pr) -> None:
    _, breakdown = score_pull_request_from_model(db_pr, _config(interest_enabled=False), "holdenk")
    assert "llm_interest" not in breakdown
