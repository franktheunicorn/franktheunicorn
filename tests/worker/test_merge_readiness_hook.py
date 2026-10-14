"""The review pipeline re-scores merge readiness once a review has run."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.config.models import OperatorConfig, ProjectConfig
from franktheunicorn.worker.runner import _refresh_merge_readiness_after_review
from tests.factories import PullRequestFactory


@pytest.mark.django_db
class TestRefreshMergeReadinessAfterReview:
    def test_rescores_with_the_operator_username(self) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner="o", repo="r")
        with patch("franktheunicorn.scoring.merge_readiness.refresh_merge_readiness") as refresh:
            _refresh_merge_readiness_after_review(pr, pc, OperatorConfig(github_username="holdenk"))
        refresh.assert_called_once_with(pr, pc, "holdenk")

    def test_no_operator_config_passes_empty_username(self) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner="o", repo="r")
        with patch("franktheunicorn.scoring.merge_readiness.refresh_merge_readiness") as refresh:
            _refresh_merge_readiness_after_review(pr, pc, None)
        refresh.assert_called_once_with(pr, pc, "")

    def test_disabled_is_a_no_op(self) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner="o", repo="r", merge_readiness={"enabled": False})
        with patch("franktheunicorn.scoring.merge_readiness.refresh_merge_readiness") as refresh:
            _refresh_merge_readiness_after_review(pr, pc, None)
        refresh.assert_not_called()

    def test_errors_are_swallowed(self) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner="o", repo="r")
        with patch(
            "franktheunicorn.scoring.merge_readiness.refresh_merge_readiness",
            side_effect=RuntimeError("db gone"),
        ):
            _refresh_merge_readiness_after_review(pr, pc, None)

    def test_real_evaluation_writes_the_verdict(self) -> None:
        pr = PullRequestFactory(head_sha="h1", mergeable=True)
        pc = ProjectConfig(owner="o", repo="r")
        _refresh_merge_readiness_after_review(pr, pc, OperatorConfig(github_username="holdenk"))
        pr.refresh_from_db()
        assert "blockers" in pr.merge_readiness_detail
