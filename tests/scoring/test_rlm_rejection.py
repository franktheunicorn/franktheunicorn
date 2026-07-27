"""Tests for the optional RLM rejection judge."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, RLMScoringConfig
from franktheunicorn.review.backends.base import PRContext, ReviewFinding, ReviewResult
from franktheunicorn.scoring.rlm_rejection import (
    _corroborates,
    combine_rejection,
    judge_rejection,
)


def test_combine_rejection_modes() -> None:
    assert combine_rejection(0.2, 0.9, "max") == 0.9
    assert combine_rejection(0.2, 0.8, "average") == pytest.approx(0.5)
    assert combine_rejection(0.2, 0.9, "rlm-only") == 0.9
    # No sklearn value → fall back to the RLM value.
    assert combine_rejection(None, 0.7, "max") == 0.7


def test_corroborates() -> None:
    original = ReviewFinding(file_path="x.py", line_number=10, body="bug")
    near = [ReviewFinding(file_path="x.py", line_number=12, body="same area")]
    far = [ReviewFinding(file_path="x.py", line_number=50, body="elsewhere")]
    other_file = [ReviewFinding(file_path="y.py", line_number=10, body="diff file")]
    assert _corroborates(original, near) is True
    assert _corroborates(original, far) is False
    assert _corroborates(original, other_file) is False


class _FakeLeaf:
    def __init__(self, result: ReviewResult) -> None:
        self._result = result

    def generate_review(self, diff: str, pr_context: PRContext) -> ReviewResult:
        return self._result


def _scoring() -> RLMScoringConfig:
    return RLMScoringConfig(rejection_judge_enabled=True, leaf=LLMBackendConfig(provider="stub"))


@pytest.mark.django_db
def test_judge_rejection_branches(db_pr) -> None:
    finding = ReviewFinding(file_path="x.py", line_number=10, body="needs a guard")
    code_context = "+++ b/x.py\n@@ -1 +1 @@\n+code\n"

    cases = [
        (ReviewResult(findings=[]), 0.70),
        (
            ReviewResult(findings=[ReviewFinding(file_path="x.py", line_number=11, body="same")]),
            0.15,
        ),
        (
            ReviewResult(findings=[ReviewFinding(file_path="z.py", line_number=1, body="other")]),
            0.55,
        ),
    ]
    for result, expected in cases:
        with patch("franktheunicorn.review.backends.get_backend", return_value=_FakeLeaf(result)):
            prob = judge_rejection(finding, code_context, db_pr, _scoring(), "standard")
        assert prob == expected


@pytest.mark.django_db
def test_judge_rejection_handles_engine_failure(db_pr) -> None:
    finding = ReviewFinding(file_path="x.py", line_number=10, body="x")
    with patch(
        "franktheunicorn.review.rlm.engine.RLMEngine.review", side_effect=RuntimeError("boom")
    ):
        prob = judge_rejection(finding, "+++ b/x.py\n", db_pr, _scoring(), "standard")
    assert prob == 0.5


@pytest.mark.django_db
def test_judge_rejection_empty_diff_is_neutral(db_pr) -> None:
    finding = ReviewFinding(file_path="", line_number=None, body="x")
    prob = judge_rejection(finding, "", db_pr, _scoring(), "standard")
    assert prob == 0.5
