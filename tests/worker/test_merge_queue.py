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
    execute_post_merge_restack,
    select_next_pr_to_restack,
    update_merge_eligibility,
    wait_for_ci_green,
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

    def test_uses_api_when_no_script(self) -> None:
        config = MergeQueueConfig(enabled=True)
        pr = PullRequestFactory(number=42)

        with patch("franktheunicorn.worker.merge_queue.execute_merge_api") as mock_api:
            mock_api.return_value = MergeResult(success=True, method="merge")
            result = execute_merge(pr, config, github_client=object())

        assert result.success is True
        mock_api.assert_called_once()

    def test_runs_restack_only_after_success_when_enabled(self) -> None:
        config = MergeQueueConfig(enabled=True, post_merge_restack_enabled=True)
        pr = PullRequestFactory(number=42)

        with (
            patch("franktheunicorn.worker.merge_queue.execute_merge_api") as mock_api,
            patch("franktheunicorn.worker.merge_queue.execute_post_merge_restack") as mock_restack,
        ):
            mock_api.return_value = MergeResult(success=True, method="merge")
            mock_restack.return_value.success = True
            mock_restack.return_value.ci_wait_state = "success"
            mock_restack.return_value.ci_wait_reason = "All required checks passed"
            result = execute_merge(pr, config, github_client=object(), repo_path="/tmp/repo")

        assert result.success is True
        mock_restack.assert_called_once()
        assert result.ci_wait_state == "success"

    def test_restack_failure_marks_merge_failed(self) -> None:
        config = MergeQueueConfig(enabled=True, post_merge_restack_enabled=True)
        pr = PullRequestFactory(number=42)

        with (
            patch("franktheunicorn.worker.merge_queue.execute_merge_api") as mock_api,
            patch("franktheunicorn.worker.merge_queue.execute_post_merge_restack") as mock_restack,
        ):
            mock_api.return_value = MergeResult(success=True, method="merge")
            mock_restack.return_value.success = False
            mock_restack.return_value.error = "Timed out waiting for required checks"
            mock_restack.return_value.ci_wait_state = "timeout"
            mock_restack.return_value.ci_wait_reason = "Timed out waiting for required checks"
            result = execute_merge(pr, config, github_client=object(), repo_path="/tmp/repo")

        assert result.success is False
        assert result.ci_wait_state == "timeout"
        assert "Timed out" in result.error


@pytest.mark.django_db
class TestRestackSelection:
    def test_select_next_pr_to_restack_orders_queue(self) -> None:
        base = PullRequestFactory(
            merge_queue_eligible=True, state="open", interest_score=1.0, number=1
        )
        PullRequestFactory(
            project=base.project,
            merge_queue_eligible=True,
            state="open",
            interest_score=9.0,
            number=2,
        )
        PullRequestFactory(
            project=base.project, merge_queue_eligible=False, state="open", interest_score=10.0
        )

        next_pr = select_next_pr_to_restack(base.project_id)
        assert next_pr is not None
        assert next_pr.number == 2


@pytest.mark.django_db
class TestPostMergeRestack:
    def test_no_next_pr_is_success(self) -> None:
        merged_pr = PullRequestFactory(state="merged")
        config = MergeQueueConfig(post_merge_restack_enabled=True)

        result = execute_post_merge_restack(merged_pr, config, "/tmp/repo")
        assert result.success is True
        assert result.pr_number is None


@pytest.mark.django_db
class TestWaitForCIGreen:
    def test_wait_success(self) -> None:
        from unittest.mock import MagicMock

        from franktheunicorn.backends.github import GitHubClient

        pr = PullRequestFactory(base_branch="main", head_sha="abc123")
        client = GitHubClient(token="fake")
        client._client = MagicMock()

        branch_resp = MagicMock(status_code=200)
        branch_resp.json.return_value = {
            "protection": {"required_status_checks": {"contexts": ["ci/test"]}}
        }
        checks_resp = MagicMock(status_code=200)
        checks_resp.json.return_value = {
            "check_runs": [{"name": "ci/test", "conclusion": "success"}]
        }
        statuses_resp = MagicMock(status_code=200)
        statuses_resp.json.return_value = {"statuses": []}
        client._client.get.side_effect = [branch_resp, checks_resp, statuses_resp]

        state, reason = wait_for_ci_green(pr, client, timeout=1, poll_interval=1)
        assert state == "success"
        assert "passed" in reason

    @patch("franktheunicorn.worker.merge_queue.time.sleep")
    def test_wait_failure(self, _: object) -> None:
        from unittest.mock import MagicMock

        from franktheunicorn.backends.github import GitHubClient

        pr = PullRequestFactory(base_branch="main", head_sha="abc123")
        client = GitHubClient(token="fake")
        client._client = MagicMock()

        branch_resp = MagicMock(status_code=200)
        branch_resp.json.return_value = {
            "protection": {"required_status_checks": {"contexts": ["ci/test"]}}
        }
        checks_resp = MagicMock(status_code=200)
        checks_resp.json.return_value = {
            "check_runs": [{"name": "ci/test", "conclusion": "failure"}]
        }
        statuses_resp = MagicMock(status_code=200)
        statuses_resp.json.return_value = {"statuses": []}
        client._client.get.side_effect = [branch_resp, checks_resp, statuses_resp]

        state, reason = wait_for_ci_green(pr, client, timeout=2, poll_interval=1)
        assert state == "failure"
        assert "Required check failed" in reason


@pytest.mark.django_db
class TestExecuteMergeScriptTimeout:
    @patch("franktheunicorn.worker.merge_queue.subprocess.run")
    def test_script_timeout(self, mock_run: object) -> None:
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired(cmd="merge.sh", timeout=300)  # type: ignore[attr-defined]
        pr = PullRequestFactory(number=42)

        result = execute_merge_script(pr, "/path/to/merge.sh")
        assert result.success is False
        assert "timed out" in result.error


@pytest.mark.django_db
class TestExecuteMergeAPI:
    def test_api_success(self) -> None:
        from unittest.mock import MagicMock

        from franktheunicorn.backends.github import GitHubClient
        from franktheunicorn.worker.merge_queue import execute_merge_api

        pr = PullRequestFactory(number=42)
        config = MergeQueueConfig(enabled=True, merge_method="squash")

        mock_response = MagicMock()
        mock_response.status_code = 200

        client = GitHubClient(token="fake")
        client._client = MagicMock()
        client._client.put.return_value = mock_response

        result = execute_merge_api(pr, config, client)

        assert result.success is True
        assert result.method == "squash"

    def test_api_failure_status(self) -> None:
        from unittest.mock import MagicMock

        from franktheunicorn.backends.github import GitHubClient
        from franktheunicorn.worker.merge_queue import execute_merge_api

        pr = PullRequestFactory(number=42)
        config = MergeQueueConfig(enabled=True, merge_method="merge")

        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_response.text = "Conflict"

        client = GitHubClient(token="fake")
        client._client = MagicMock()
        client._client.put.return_value = mock_response

        result = execute_merge_api(pr, config, client)

        assert result.success is False
        assert "409" in result.error

    def test_api_not_github_client(self) -> None:
        from franktheunicorn.worker.merge_queue import execute_merge_api

        pr = PullRequestFactory(number=42)
        config = MergeQueueConfig(enabled=True)

        result = execute_merge_api(pr, config, object())
        assert result.success is False
        assert "not available" in result.error

    def test_api_exception(self) -> None:
        from unittest.mock import MagicMock

        from franktheunicorn.backends.github import GitHubClient
        from franktheunicorn.worker.merge_queue import execute_merge_api

        pr = PullRequestFactory(number=42)
        config = MergeQueueConfig(enabled=True)

        client = GitHubClient(token="fake")
        client._client = MagicMock()
        client._client.put.side_effect = Exception("network error")

        result = execute_merge_api(pr, config, client)

        assert result.success is False
