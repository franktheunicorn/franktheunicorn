"""Tests for the worker-side WorkerCommand dispatcher."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.core.models import PullRequest, SecurityReport, WorkerCommand
from franktheunicorn.worker.commands import process_pending_commands


@pytest.mark.django_db
class TestProcessPendingCommands:
    def test_no_pending_returns_zero(self) -> None:
        operator_config = MagicMock()
        assert process_pending_commands(operator_config) == 0

    def test_dual_tests_dispatches_to_test_runner(self, db_pr: PullRequest) -> None:
        operator_config = MagicMock()

        cmd = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=db_pr,
        )

        mock_pc = MagicMock()
        mock_pc.tests.enabled = True
        mock_runner = MagicMock()
        mock_test_run = MagicMock(pk=42, differential_verdict="good")
        mock_runner.run_differential_test.return_value = mock_test_run

        with (
            patch(
                "franktheunicorn.config.loader.get_project_config",
                return_value=mock_pc,
            ),
            patch(
                "franktheunicorn.worker.test_runner.TestRunner",
                return_value=mock_runner,
            ),
        ):
            processed = process_pending_commands(operator_config)

        assert processed == 1
        cmd.refresh_from_db()
        assert cmd.status == "completed"
        assert cmd.error == ""
        assert cmd.started_at is not None
        assert cmd.finished_at is not None
        assert "verdict=good" in cmd.log
        mock_runner.run_differential_test.assert_called_once()
        # The worker forces the run regardless of trusted-author gate.
        _, kwargs = mock_runner.run_differential_test.call_args
        assert kwargs.get("force") is True

    def test_dual_tests_failure_marks_command_failed(self, db_pr: PullRequest) -> None:
        operator_config = MagicMock()
        cmd = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=db_pr,
        )
        mock_pc = MagicMock()
        mock_pc.tests.enabled = True
        mock_runner = MagicMock()
        mock_runner.run_differential_test.side_effect = RuntimeError("docker exploded")

        with (
            patch(
                "franktheunicorn.config.loader.get_project_config",
                return_value=mock_pc,
            ),
            patch(
                "franktheunicorn.worker.test_runner.TestRunner",
                return_value=mock_runner,
            ),
        ):
            processed = process_pending_commands(operator_config)

        assert processed == 1
        cmd.refresh_from_db()
        assert cmd.status == "failed"
        assert "docker exploded" in cmd.error
        assert cmd.finished_at is not None

    def test_dual_tests_rejects_when_tests_disabled(self, db_pr: PullRequest) -> None:
        operator_config = MagicMock()
        cmd = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=db_pr,
        )
        mock_pc = MagicMock()
        mock_pc.tests.enabled = False
        with patch(
            "franktheunicorn.config.loader.get_project_config",
            return_value=mock_pc,
        ):
            process_pending_commands(operator_config)

        cmd.refresh_from_db()
        assert cmd.status == "failed"
        assert "not enabled" in cmd.error

    def test_run_agents_dispatches_to_process_pr(self, db_pr: PullRequest) -> None:
        operator_config = MagicMock()
        cmd = WorkerCommand.objects.create(
            command="run_agents",
            pull_request=db_pr,
        )
        mock_pc = MagicMock()

        def fake_process_pr(pr, project_config, opc, repo_path=None, *, force, log_lines):
            log_lines.append("ran agent A")
            return [MagicMock(), MagicMock(), MagicMock()]

        with (
            patch(
                "franktheunicorn.config.loader.get_project_config",
                return_value=mock_pc,
            ),
            patch(
                "franktheunicorn.worker.runner.process_pr",
                side_effect=fake_process_pr,
            ),
        ):
            process_pending_commands(operator_config)

        cmd.refresh_from_db()
        assert cmd.status == "completed"
        assert "3 finding" in cmd.log

    def test_security_sandbox_dispatch(self) -> None:
        from franktheunicorn.core.models import Project

        operator_config = MagicMock()
        project = Project.objects.create(owner="acme", repo="widgets")
        report = SecurityReport.objects.create(
            project=project,
            title="CVE thing",
            raw_text="",
        )
        cmd = WorkerCommand.objects.create(
            command="run_security_sandbox",
            security_report=report,
        )

        mock_result = MagicMock(verdict="safe", output="all good")
        with patch(
            "franktheunicorn.security.sandbox.run_poc_in_sandbox",
            return_value=mock_result,
        ):
            process_pending_commands(operator_config)

        cmd.refresh_from_db()
        report.refresh_from_db()
        assert cmd.status == "completed"
        assert report.sandbox_requested is True
        assert report.sandbox_verdict == "safe"
        assert report.sandbox_result == "all good"

    def test_unknown_command_marked_failed(self, db_pr: PullRequest) -> None:
        operator_config = MagicMock()
        cmd = WorkerCommand.objects.create(
            command="run_dual_tests",  # use a valid choice for the field
            pull_request=db_pr,
        )
        # Bypass model validation by raw-updating the command field to a
        # value the dispatcher doesn't know about. Simulates a future
        # command type rolled out before the worker is upgraded.
        WorkerCommand.objects.filter(pk=cmd.pk).update(command="unknown_thing")

        process_pending_commands(operator_config)

        cmd.refresh_from_db()
        assert cmd.status == "failed"
        assert "Unknown WorkerCommand" in cmd.error

    def test_already_running_command_is_skipped(self, db_pr: PullRequest) -> None:
        operator_config = MagicMock()
        # A command already in flight must not be re-claimed mid-run by
        # select_for_update. (Rows orphaned by a *dead* worker are recovered
        # separately: requeue_interrupted_commands runs at worker startup.)
        cmd = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=db_pr,
        )
        WorkerCommand.objects.filter(pk=cmd.pk).update(status="running")

        with patch("franktheunicorn.worker.commands._dispatch") as mock_dispatch:
            process_pending_commands(operator_config)

        mock_dispatch.assert_not_called()

    def test_requeue_interrupted_commands(self, db_pr: PullRequest) -> None:
        from franktheunicorn.worker.commands import requeue_interrupted_commands

        orphaned = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=db_pr,
        )
        WorkerCommand.objects.filter(pk=orphaned.pk).update(status="running")
        done = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=db_pr,
        )
        WorkerCommand.objects.filter(pk=done.pk).update(status="completed")

        count = requeue_interrupted_commands()

        assert count == 1
        orphaned.refresh_from_db()
        done.refresh_from_db()
        assert orphaned.status == "pending"
        assert orphaned.started_at is None
        assert done.status == "completed"

    def test_keyboard_interrupt_marks_command_failed(self, db_pr: PullRequest) -> None:
        """SIGTERM (KeyboardInterrupt) mid-dispatch must not strand the row
        in status="running" — the worker converts SIGTERM to
        KeyboardInterrupt, which except Exception does not catch."""
        import pytest

        operator_config = MagicMock()
        cmd = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=db_pr,
        )

        with (
            patch(
                "franktheunicorn.worker.commands._dispatch",
                side_effect=KeyboardInterrupt,
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            process_pending_commands(operator_config)

        cmd.refresh_from_db()
        assert cmd.status == "failed"
        assert "Interrupted" in cmd.error
        assert cmd.finished_at is not None

    def test_processes_in_creation_order(self, db_pr: PullRequest) -> None:
        operator_config = MagicMock()
        first = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=db_pr,
        )
        second = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=db_pr,
        )

        order: list[int] = []

        def record(cmd, _opc):
            order.append(cmd.pk)

        with patch("franktheunicorn.worker.commands._dispatch", side_effect=record):
            process_pending_commands(operator_config)

        assert order == [first.pk, second.pk]
