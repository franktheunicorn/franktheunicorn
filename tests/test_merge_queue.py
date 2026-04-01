"""Tests for merge queue (v2)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.config.models import MergeQueueConfig
from franktheunicorn.worker.merge_queue import (
    MergeResult,
    evaluate_merge_eligibility,
    execute_merge,
    execute_merge_script,
    update_merge_eligibility,
)
from tests.factories import PullRequestFactory


class TestEvaluateMergeEligibility:
    def test_all_checks_pass(self) -> None:
        pr = PullRequestFactory.build(
            ci_status="pass",
            approval_count=2,
            mergeable=True,
        )
        config = MergeQueueConfig(enabled=True, required_approvals=2)
        result = evaluate_merge_eligibility(pr, config)

        assert result.eligible is True
        assert result.ci_pass is True
        assert result.approvals_met is True
        assert result.no_conflicts is True

    def test_ci_fails(self) -> None:
        pr = PullRequestFactory.build(
            ci_status="fail",
            approval_count=2,
            mergeable=True,
        )
        config = MergeQueueConfig(enabled=True, required_approvals=1)
        result = evaluate_merge_eligibility(pr, config)

        assert result.eligible is False
        assert result.ci_pass is False

    def test_insufficient_approvals(self) -> None:
        pr = PullRequestFactory.build(
            ci_status="pass",
            approval_count=1,
            mergeable=True,
        )
        config = MergeQueueConfig(enabled=True, required_approvals=2)
        result = evaluate_merge_eligibility(pr, config)

        assert result.eligible is False
        assert result.approvals_met is False

    def test_has_conflicts(self) -> None:
        pr = PullRequestFactory.build(
            ci_status="pass",
            approval_count=2,
            mergeable=False,
        )
        config = MergeQueueConfig(enabled=True, required_approvals=1)
        result = evaluate_merge_eligibility(pr, config)

        assert result.eligible is False
        assert result.no_conflicts is False

    def test_mergeable_unknown(self) -> None:
        pr = PullRequestFactory.build(
            ci_status="pass",
            approval_count=2,
            mergeable=None,
        )
        config = MergeQueueConfig(enabled=True, required_approvals=1)
        result = evaluate_merge_eligibility(pr, config)

        assert result.eligible is False
        assert result.no_conflicts is False
        assert any("unknown" in d for d in result.details)

    def test_ci_check_disabled(self) -> None:
        pr = PullRequestFactory.build(
            ci_status="fail",
            approval_count=1,
            mergeable=True,
        )
        config = MergeQueueConfig(enabled=True, required_approvals=1, require_ci_pass=False)
        result = evaluate_merge_eligibility(pr, config)

        assert result.eligible is True
        assert result.ci_pass is True

    def test_conflict_check_disabled(self) -> None:
        pr = PullRequestFactory.build(
            ci_status="pass",
            approval_count=1,
            mergeable=False,
        )
        config = MergeQueueConfig(enabled=True, required_approvals=1, require_no_conflicts=False)
        result = evaluate_merge_eligibility(pr, config)

        assert result.eligible is True
        assert result.no_conflicts is True


@pytest.mark.django_db
class TestUpdateMergeEligibility:
    def test_persists_eligibility(self) -> None:
        pr = PullRequestFactory(ci_status="pass", approval_count=2, mergeable=True)
        config = MergeQueueConfig(enabled=True, required_approvals=1)

        result = update_merge_eligibility(pr, config)
        pr.refresh_from_db()

        assert result.eligible is True
        assert pr.merge_queue_eligible is True


@pytest.mark.django_db
class TestExecuteMergeScript:
    @patch("franktheunicorn.worker.merge_queue.subprocess.run")
    def test_successful_script(self, mock_run: object) -> None:
        mock_run.return_value = type(  # type: ignore[attr-defined]
            "Result", (), {"returncode": 0, "stdout": "Merged!", "stderr": ""}
        )()
        pr = PullRequestFactory(number=42)

        result = execute_merge_script(pr, "/path/to/merge.sh")
        assert result.success is True
        assert result.method == "script"

    @patch("franktheunicorn.worker.merge_queue.subprocess.run")
    def test_script_failure(self, mock_run: object) -> None:
        mock_run.return_value = type(  # type: ignore[attr-defined]
            "Result", (), {"returncode": 1, "stdout": "", "stderr": "Access denied"}
        )()
        pr = PullRequestFactory(number=42)

        result = execute_merge_script(pr, "/path/to/merge.sh")
        assert result.success is False
        assert "Access denied" in result.error

    def test_script_not_found(self) -> None:
        pr = PullRequestFactory(number=42)

        result = execute_merge_script(pr, "/nonexistent/merge.sh")
        assert result.success is False
        assert "not found" in result.error


@pytest.mark.django_db
class TestExecuteMerge:
    def test_uses_script_when_configured(self) -> None:
        config = MergeQueueConfig(merge_script="/path/to/merge.sh")
        pr = PullRequestFactory(number=42)

        with patch("franktheunicorn.worker.merge_queue.execute_merge_script") as mock_script:
            mock_script.return_value = MergeResult(success=True, method="script")
            result = execute_merge(pr, config)

        assert result.success is True
        mock_script.assert_called_once()

    def test_no_method_available(self) -> None:
        config = MergeQueueConfig()
        pr = PullRequestFactory(number=42)

        result = execute_merge(pr, config)
        assert result.success is False
        assert "No merge method" in result.error
