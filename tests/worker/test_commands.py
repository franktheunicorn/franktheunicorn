"""Tests for the worker-side WorkerCommand dispatcher."""

from __future__ import annotations

from typing import Any
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

        def fake_process_pr(
            pr, project_config, opc, repo_path=None, *, forge_client=None, force, log_lines
        ):
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

    def test_security_triage_dispatch(self) -> None:
        """The dashboard queues triage; the worker is what actually runs it."""
        from franktheunicorn.core.models import Project

        operator_config = MagicMock()
        project = Project.objects.create(owner="acme", repo="widgets")
        report = SecurityReport.objects.create(project=project, title="XSS in UI", raw_text="...")
        cmd = WorkerCommand.objects.create(
            command="run_security_triage",
            security_report=report,
        )
        mock_pc = MagicMock()

        def fake_triage(rpt, project_config, opc):
            assert project_config is mock_pc
            SecurityReport.objects.filter(pk=rpt.pk).update(
                assessed_severity="medium", status="triaged"
            )
            return rpt

        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=mock_pc),
            patch("franktheunicorn.security.triage.triage_report", side_effect=fake_triage),
        ):
            processed = process_pending_commands(operator_config)

        assert processed == 1
        cmd.refresh_from_db()
        assert cmd.status == "completed"
        # The log reflects the *stored* outcome, not the pre-triage row.
        assert "severity='medium'" in cmd.log
        assert "status='triaged'" in cmd.log

    def test_security_triage_without_report_fails(self, db_pr: PullRequest) -> None:
        operator_config = MagicMock()
        cmd = WorkerCommand.objects.create(command="run_dual_tests", pull_request=db_pr)
        WorkerCommand.objects.filter(pk=cmd.pk).update(
            command="run_security_triage", pull_request=None
        )

        process_pending_commands(operator_config)

        cmd.refresh_from_db()
        assert cmd.status == "failed"
        assert "requires a security_report" in cmd.error

    def test_security_triage_failure_marks_command_failed(self) -> None:
        from franktheunicorn.core.models import Project

        operator_config = MagicMock()
        project = Project.objects.create(owner="acme", repo="widgets")
        report = SecurityReport.objects.create(project=project, title="thing", raw_text="")
        cmd = WorkerCommand.objects.create(
            command="run_security_triage",
            security_report=report,
        )

        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=None),
            patch(
                "franktheunicorn.security.triage.triage_report",
                side_effect=RuntimeError("llm exploded"),
            ),
        ):
            process_pending_commands(operator_config)

        cmd.refresh_from_db()
        assert cmd.status == "failed"
        assert "llm exploded" in cmd.error

    def test_security_triage_works_without_project(self) -> None:
        """A report pasted with no project attached still triages."""
        operator_config = MagicMock()
        report = SecurityReport.objects.create(title="orphan report", raw_text="")
        cmd = WorkerCommand.objects.create(
            command="run_security_triage",
            security_report=report,
        )

        with patch(
            "franktheunicorn.security.triage.triage_report", return_value=report
        ) as mock_triage:
            process_pending_commands(operator_config)

        cmd.refresh_from_db()
        assert cmd.status == "completed"
        assert mock_triage.call_args.args[1] is None

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


class TestForgeClientFor:
    """``_forge_client_for`` builds the project's forge client for the dashboard
    "Force Run Agents" path, or returns None when the forge can't be resolved —
    a diff-fetch setup problem must never hard-fail the trigger."""

    def test_builds_client_for_registered_forge(self) -> None:
        from franktheunicorn.backends.github import GitHubClient
        from franktheunicorn.config.models import (
            ForgeRegistryEntry,
            OperatorConfig,
            ProjectConfig,
        )
        from franktheunicorn.worker.commands import _forge_client_for

        oc = OperatorConfig(forges=[ForgeRegistryEntry(name="github", type="github", token="t")])
        pc = ProjectConfig(owner="acme", repo="widgets", forge="github")

        assert isinstance(_forge_client_for(pc, oc), GitHubClient)

    def test_returns_none_for_unregistered_forge(self) -> None:
        from franktheunicorn.config.models import OperatorConfig, ProjectConfig
        from franktheunicorn.worker.commands import _forge_client_for

        oc = OperatorConfig(forges=[])
        pc = ProjectConfig(owner="acme", repo="widgets", forge="ghe-internal")

        assert _forge_client_for(pc, oc) is None


class TestMidCycleDrain:
    """``_drain_worker_commands`` keeps operator-triggered work responsive.

    Without it, a queued security triage waits for the whole poll cycle to
    finish, which on a busy repo is minutes — while the dashboard promises
    "within seconds".
    """

    def test_drains_pending_commands(self) -> None:
        from franktheunicorn.worker import runner

        operator_config = MagicMock()
        with patch(
            "franktheunicorn.worker.commands.process_pending_commands",
            return_value=2,
        ) as mock_process:
            runner._drain_worker_commands(operator_config)

        mock_process.assert_called_once_with(operator_config)

    def test_no_operator_config_is_a_noop(self) -> None:
        from franktheunicorn.worker import runner

        with patch("franktheunicorn.worker.commands.process_pending_commands") as mock_process:
            runner._drain_worker_commands(None)

        mock_process.assert_not_called()

    @pytest.mark.django_db
    def test_cycle_drains_even_with_nothing_to_poll(self) -> None:
        """The steady state is an empty poll, and that's when this matters.

        With the unchanged-PR skip, poll_project returns [] for every project,
        so a drain that only ran per-PR would never fire — while the cycle
        still does the mention scan, the backfill and the alert sweep.
        """
        from franktheunicorn.config.models import OperatorConfig
        from franktheunicorn.worker import runner

        operator_config = OperatorConfig(github_username="holdenk")
        with patch.object(runner, "_drain_worker_commands") as mock_drain:
            runner._run_cycle({}, [], "holdenk", operator_config)

        assert mock_drain.call_count >= 1
        assert mock_drain.call_args.args == (operator_config,)

    def test_failure_does_not_abort_the_poll(self) -> None:
        from franktheunicorn.worker import runner

        with patch(
            "franktheunicorn.worker.commands.process_pending_commands",
            side_effect=RuntimeError("db locked"),
        ):
            runner._drain_worker_commands(MagicMock())  # must not raise


@pytest.mark.django_db
class TestBackfillEligibility:
    """A skipped PR with no drafts must stay reviewable.

    The poller's unchanged-PR skip means process_pr never sees it, so if the
    backfill also excludes it, nothing reviews it until upstream moves.
    """

    def test_skipped_pr_without_drafts_reaches_the_backfill(self) -> None:
        from franktheunicorn.config.models import ProjectConfig
        from franktheunicorn.core.models import Project, PullRequest
        from franktheunicorn.worker import runner

        project = Project.objects.create(owner="apache", repo="spark")
        pr = PullRequest.objects.create(
            project=project,
            github_id=1,
            number=42,
            title="ingested by lookup_pr, never reviewed",
            author="someone",
            state="open",
            base_sha="a" * 40,
            score_breakdown={"x": 1},
        )
        pc = ProjectConfig(owner="apache", repo="spark")

        seen: list[int] = []

        def fake_process_pr(pr_arg, *args: Any, **kwargs: Any):
            seen.append(pr_arg.pk)
            return []

        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.worker.runner.process_pr", side_effect=fake_process_pr),
        ):
            runner._backfill_unreviewed_prs(
                already_polled_pks=set(),  # the poll skipped it, so it isn't here
                project_configs=[pc],
                operator_config=None,
                disabled_backends=frozenset(),
                diff_http=MagicMock(),
            )

        assert seen == [pr.pk]
