"""Tests for the RLM rejection-judge gate in the drafter."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, RLMScoringConfig
from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.drafter import create_drafts_from_findings


def _finding() -> ReviewFinding:
    return ReviewFinding(
        file_path="x.py",
        line_number=10,
        title="correctness issue",
        body="This needs a null check.",
        severity="important",
    )


def _scoring(**kwargs: object) -> RLMScoringConfig:
    return RLMScoringConfig(
        rejection_judge_enabled=True,
        leaf=LLMBackendConfig(provider="stub"),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.django_db
def test_rejection_judge_disabled_leaves_probability_none(db_pr) -> None:
    drafts = create_drafts_from_findings(
        db_pr, [_finding()], source="agent", project=db_pr.project, rlm_scoring=None
    )
    assert drafts[0].rejection_probability is None


@pytest.mark.django_db
def test_rejection_judge_sets_probability_and_suppresses(db_pr) -> None:
    # No sklearn model exists, so the combined value is purely the RLM judge's.
    with patch("franktheunicorn.scoring.rlm_rejection.judge_rejection", return_value=0.9):
        drafts = create_drafts_from_findings(
            db_pr,
            [_finding()],
            source="agent",
            project=db_pr.project,
            rlm_scoring=_scoring(),
        )
    assert drafts[0].rejection_probability == 0.9
    assert drafts[0].is_auto_suppressed is True


@pytest.mark.django_db
def test_rejection_judge_low_probability_not_suppressed(db_pr) -> None:
    with patch("franktheunicorn.scoring.rlm_rejection.judge_rejection", return_value=0.2):
        drafts = create_drafts_from_findings(
            db_pr,
            [_finding()],
            source="agent",
            project=db_pr.project,
            rlm_scoring=_scoring(),
        )
    assert drafts[0].rejection_probability == 0.2
    assert drafts[0].is_auto_suppressed is False
