"""Tests for the worker-side WorkerCommand dispatcher."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from franktheunicorn.core.models import PullRequest, SecurityReport, WorkerCommand
from franktheunicorn.worker.commands import _dispatch, process_pending_commands
from tests.factories import (
    ProjectFactory,
    PullRequestFactory,
    SecurityReportFactory,
    make_operator_config,
)


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
        operator_config = MagicMock()
        project = ProjectFactory(owner="acme", repo="widgets")
        report = SecurityReportFactory(project=project, title="CVE thing", raw_text="")
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
        operator_config = MagicMock()
        project = ProjectFactory(owner="acme", repo="widgets")
        report = SecurityReportFactory(project=project, title="XSS in UI", raw_text="...")
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
        operator_config = MagicMock()
        project = ProjectFactory(owner="acme", repo="widgets")
        report = SecurityReportFactory(project=project, title="thing", raw_text="")
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
        report = SecurityReportFactory(title="orphan report", raw_text="")
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
        # A different command, because unique_inflight_command_per_pr forbids two
        # in-flight rows of one kind per PR — which is the point of it.
        done = WorkerCommand.objects.create(
            command="run_agents",
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
        first = WorkerCommand.objects.create(command="run_dual_tests", pull_request=db_pr)
        second = WorkerCommand.objects.create(command="run_agents", pull_request=db_pr)

        order: list[int] = []

        def record(cmd, _opc):
            order.append(cmd.pk)

        with patch("franktheunicorn.worker.commands._dispatch", side_effect=record):
            process_pending_commands(operator_config)

        assert order == [first.pk, second.pk]


@pytest.mark.django_db
class TestCommandPriority:
    """A shared FIFO queue makes the operator wait behind every bulk import.

    One archive imported with --triage is a thousand run_security_triage rows, at
    an NVD lookup plus two LLM calls each. Force Run Agents is a click with
    someone watching, and it used to land behind all of them.
    """

    def test_interactive_runs_before_bulk(self, db_pr: PullRequest) -> None:
        from franktheunicorn.security.queue import PRIORITY_INTERACTIVE

        operator_config = MagicMock()
        bulk = [
            WorkerCommand.objects.create(
                command="run_security_triage", security_report=SecurityReportFactory()
            )
            for _ in range(3)
        ]
        clicked = WorkerCommand.objects.create(
            command="run_agents", pull_request=db_pr, priority=PRIORITY_INTERACTIVE
        )

        order: list[int] = []
        with patch(
            "franktheunicorn.worker.commands._dispatch",
            side_effect=lambda cmd, _opc: order.append(cmd.pk),
        ):
            process_pending_commands(operator_config)

        assert order[0] == clicked.pk, "the click goes first"
        assert sorted(order[1:]) == sorted(c.pk for c in bulk)

    def test_a_click_mid_drain_jumps_the_rest_of_the_batch(self, db_pr: PullRequest) -> None:
        """The drain used to snapshot the pending set up front, which fixed the
        running order at the moment it started — so a click that arrived while a
        backlog was draining went to the back of it whatever its priority."""
        from franktheunicorn.security.queue import PRIORITY_INTERACTIVE

        operator_config = MagicMock()
        bulk = [
            WorkerCommand.objects.create(
                command="run_security_triage", security_report=SecurityReportFactory()
            )
            for _ in range(4)
        ]
        clicked: list[int] = []
        order: list[int] = []

        def record(cmd, _opc):
            order.append(cmd.pk)
            if len(order) == 1:
                # The operator clicks Force Run while the first bulk item runs.
                row = WorkerCommand.objects.create(
                    command="run_agents", pull_request=db_pr, priority=PRIORITY_INTERACTIVE
                )
                clicked.append(row.pk)

        with patch("franktheunicorn.worker.commands._dispatch", side_effect=record):
            process_pending_commands(operator_config)

        assert order[1] == clicked[0], "next up, not last"
        assert len(order) == len(bulk) + 1

    def test_a_drain_is_bounded_so_the_poll_cycle_keeps_its_turn(self) -> None:
        """An unbounded drain sat in one call for a thousand triage runs, during
        which the cycle made no progress and a SIGTERM waited."""
        from franktheunicorn.worker.commands import MAX_COMMANDS_PER_DRAIN

        operator_config = MagicMock()
        for _ in range(MAX_COMMANDS_PER_DRAIN + 5):
            WorkerCommand.objects.create(
                command="run_security_triage", security_report=SecurityReportFactory()
            )

        with patch("franktheunicorn.worker.commands._dispatch"):
            processed = process_pending_commands(operator_config)

        assert processed == MAX_COMMANDS_PER_DRAIN
        assert WorkerCommand.objects.filter(status="pending").count() == 5

    def test_the_backlog_still_drains_across_calls(self) -> None:
        operator_config = MagicMock()
        for _ in range(3):
            WorkerCommand.objects.create(
                command="run_security_triage", security_report=SecurityReportFactory()
            )

        with patch("franktheunicorn.worker.commands._dispatch"):
            process_pending_commands(operator_config, limit=2)
            process_pending_commands(operator_config, limit=2)

        assert WorkerCommand.objects.filter(status="pending").count() == 0


@pytest.mark.django_db
class TestInFlightDedup:
    """Force Run Agents is a 30-120s pipeline behind a button that says "reload in
    a few minutes", so an impatient operator used to queue a run per click."""

    def test_a_second_click_does_not_queue_a_second_run(self, db_pr: PullRequest) -> None:
        from franktheunicorn.security.queue import queue_command

        assert queue_command("run_agents", pull_request=db_pr) is True
        assert queue_command("run_agents", pull_request=db_pr) is False
        assert WorkerCommand.objects.filter(command="run_agents").count() == 1

    def test_a_different_command_on_the_same_pr_is_fine(self, db_pr: PullRequest) -> None:
        from franktheunicorn.security.queue import queue_command

        assert queue_command("run_agents", pull_request=db_pr) is True
        assert queue_command("run_dual_tests", pull_request=db_pr) is True

    def test_the_same_command_on_another_pr_is_fine(self, db_pr: PullRequest) -> None:
        from franktheunicorn.security.queue import queue_command

        other = PullRequestFactory(project=db_pr.project, number=db_pr.number + 1)

        assert queue_command("run_agents", pull_request=db_pr) is True
        assert queue_command("run_agents", pull_request=other) is True

    def test_a_finished_run_does_not_block_a_new_one(self, db_pr: PullRequest) -> None:
        from franktheunicorn.security.queue import queue_command

        queue_command("run_agents", pull_request=db_pr)
        WorkerCommand.objects.filter(command="run_agents").update(status="completed")

        assert queue_command("run_agents", pull_request=db_pr) is True

    def test_exactly_one_target_is_required(self, db_pr: PullRequest) -> None:
        from franktheunicorn.security.queue import queue_command

        with pytest.raises(ValueError, match="exactly one"):
            queue_command("run_agents")
        with pytest.raises(ValueError, match="exactly one"):
            queue_command("run_agents", SecurityReportFactory(), pull_request=db_pr)


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

    @pytest.mark.django_db
    def test_drains_pending_commands(self) -> None:
        from franktheunicorn.worker import runner

        operator_config = MagicMock()
        # django_db because the drain now also sweeps commands stuck in 'running'
        # before claiming new ones, which is a real query.
        with patch(
            "franktheunicorn.worker.commands.process_pending_commands",
            return_value=2,
        ) as mock_process:
            runner._drain_worker_commands(operator_config)

        mock_process.assert_called_once_with(operator_config)

    @pytest.mark.django_db
    def test_the_drain_sweeps_stuck_commands_before_claiming_new_ones(self) -> None:
        """Order matters: a row stuck in 'running' dedups every retry of that
        command, so it has to be cleared before the queue is asked for work."""
        from franktheunicorn.worker import runner

        with (
            patch("franktheunicorn.worker.commands.fail_stuck_commands") as mock_sweep,
            patch(
                "franktheunicorn.worker.commands.process_pending_commands", return_value=0
            ) as mock_process,
        ):
            runner._drain_worker_commands(MagicMock())

        mock_sweep.assert_called_once()
        assert mock_sweep.call_count == 1
        mock_process.assert_called_once()

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
        from franktheunicorn.worker import runner

        pr = PullRequestFactory(
            project=ProjectFactory(owner="apache", repo="spark"),
            number=42,
            title="ingested by lookup_pr, never reviewed",
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


@pytest.mark.django_db
class TestStuckCommands:
    """The gap requeue_interrupted_commands leaves.

    That one clears rows orphaned by a *crash*, and only at startup. A handler that
    hangs in a worker that stays up left its row 'running' forever, and the
    in-flight constraint then deduped every retry of that command — the operator was
    told "Reused in-flight" for a run that would never finish. started_at was
    already recorded and nothing read it.
    """

    def _running(self, *, age_seconds: int) -> WorkerCommand:
        report = SecurityReportFactory()
        cmd = WorkerCommand.objects.create(
            command="run_security_triage", security_report=report, status="running"
        )
        WorkerCommand.objects.filter(pk=cmd.pk).update(
            started_at=timezone.now() - timedelta(seconds=age_seconds)
        )
        cmd.refresh_from_db()
        return cmd

    def test_a_command_running_past_the_timeout_is_failed(self) -> None:
        from franktheunicorn.worker.commands import (
            STUCK_COMMAND_TIMEOUT_SECONDS,
            fail_stuck_commands,
        )

        cmd = self._running(age_seconds=STUCK_COMMAND_TIMEOUT_SECONDS + 60)

        assert fail_stuck_commands() == 1

        cmd.refresh_from_db()
        assert cmd.status == "failed"
        assert "gave up" in cmd.error
        assert cmd.finished_at is not None

    def test_a_command_inside_the_timeout_is_left_alone(self) -> None:
        from franktheunicorn.worker.commands import (
            STUCK_COMMAND_TIMEOUT_SECONDS,
            fail_stuck_commands,
        )

        cmd = self._running(age_seconds=STUCK_COMMAND_TIMEOUT_SECONDS - 60)

        assert fail_stuck_commands() == 0

        cmd.refresh_from_db()
        assert cmd.status == "running"

    def test_failing_it_frees_the_button_again(self) -> None:
        """The point of the whole thing: the in-flight constraint dedups against
        pending/running, so until that row moves the operator's click does nothing."""
        from franktheunicorn.security import queue
        from franktheunicorn.worker.commands import (
            STUCK_COMMAND_TIMEOUT_SECONDS,
            fail_stuck_commands,
        )

        cmd = self._running(age_seconds=STUCK_COMMAND_TIMEOUT_SECONDS + 60)
        report = cmd.security_report
        assert report is not None
        assert queue.queue_triage(report) is False  # deduped against the hung row

        fail_stuck_commands()

        assert queue.queue_triage(report) is True

    def test_a_row_with_no_started_at_is_not_swept(self) -> None:
        """Belt and braces: requeue_interrupted_commands nulls started_at when it
        requeues, and a pending row is not this function's business."""
        from franktheunicorn.worker.commands import fail_stuck_commands

        report = SecurityReportFactory()
        cmd = WorkerCommand.objects.create(
            command="run_security_triage", security_report=report, status="running"
        )
        WorkerCommand.objects.filter(pk=cmd.pk).update(started_at=None)

        assert fail_stuck_commands() == 0

    def test_it_names_the_target_in_the_log(self, caplog: Any) -> None:
        """The queryset is empty afterwards, so the log line is the only record of
        which target got abandoned."""
        from franktheunicorn.worker.commands import (
            STUCK_COMMAND_TIMEOUT_SECONDS,
            fail_stuck_commands,
        )

        cmd = self._running(age_seconds=STUCK_COMMAND_TIMEOUT_SECONDS + 60)

        with caplog.at_level(logging.WARNING):
            fail_stuck_commands()

        assert f"#{cmd.pk}" in caplog.text
        assert "run_security_triage" in caplog.text


@pytest.mark.django_db
class TestInteractiveDrain:
    """The interactive thread must not get stuck behind the bulk backlog."""

    def test_min_priority_skips_bulk_rows(self, db_pr: PullRequest) -> None:
        from franktheunicorn.security.queue import PRIORITY_BULK, PRIORITY_INTERACTIVE

        # Separate PRs: the queue's in-flight dedup allows one pending row per
        # (command, target), so both rows on one PR is not a reachable state.
        bulk = WorkerCommand.objects.create(
            command="run_dual_tests", pull_request=db_pr, priority=PRIORITY_BULK
        )
        interactive = WorkerCommand.objects.create(
            command="run_dual_tests",
            pull_request=PullRequestFactory(project=db_pr.project, number=db_pr.number + 1),
            priority=PRIORITY_INTERACTIVE,
        )

        with patch("franktheunicorn.worker.commands._dispatch") as dispatch:
            processed = process_pending_commands(MagicMock(), min_priority=PRIORITY_INTERACTIVE)

        assert processed == 1
        ran = [call.args[0].pk for call in dispatch.call_args_list]
        assert ran == [interactive.pk]
        bulk.refresh_from_db()
        assert bulk.status == "pending"

    def test_each_pass_closes_its_connection_then_the_event_stops_the_loop(self) -> None:
        """A per-thread SQLite handle left open sits across the cycle's writes."""
        import threading

        from franktheunicorn.security.queue import PRIORITY_INTERACTIVE
        from franktheunicorn.worker.runner import _interactive_drain_loop

        stop = threading.Event()

        def _once(*_a: Any, **_k: Any) -> int:
            stop.set()
            return 0

        with (
            patch(
                "franktheunicorn.worker.commands.process_pending_commands", side_effect=_once
            ) as drain,
            patch("django.db.connection.close") as close,
        ):
            _interactive_drain_loop(MagicMock(), stop)

        assert drain.call_count == 1
        assert drain.call_args.kwargs["min_priority"] == PRIORITY_INTERACTIVE
        assert close.call_count == 1

    def test_a_failing_command_does_not_kill_the_thread(self) -> None:
        """This thread dying silently is worse than the delay it exists to fix."""
        import threading

        from franktheunicorn.worker.runner import _interactive_drain_loop

        stop = threading.Event()
        calls: list[int] = []

        def _boom(*_a: Any, **_k: Any) -> int:
            calls.append(1)
            if len(calls) >= 2:
                stop.set()
            msg = "database is locked"
            raise RuntimeError(msg)

        with (
            patch("franktheunicorn.worker.commands.process_pending_commands", side_effect=_boom),
            patch("franktheunicorn.worker.runner.INTERACTIVE_POLL_SECONDS", 0),
            patch("django.db.connection.close"),
        ):
            _interactive_drain_loop(MagicMock(), stop)

        assert len(calls) == 2


class TestPollSecurityRechecks:
    """The handler stays short and hands the waiting back to the queue.

    It is queued at PRIORITY_INTERACTIVE, and the runner gives that priority a
    dedicated thread precisely so a click does not sit behind bulk work. The old
    handler blocked in a poll loop until ``recheck_timeout_seconds`` — 3600 by
    default, nearly all of it asleep — which parked that thread for the hour.
    """

    @pytest.mark.django_db
    def test_runs_still_going_are_re_queued(self) -> None:
        cmd = WorkerCommand.objects.create(command="poll_security_rechecks", status="running")
        with (
            patch(
                "franktheunicorn.security.recheck.poll_rechecks",
                return_value=(0, 0, 2),
            ),
            patch("franktheunicorn.worker.commands.time.sleep") as mock_sleep,
        ):
            _dispatch(cmd, make_operator_config())
        assert "2 still running" in cmd.log
        assert "re-queued" in cmd.log
        # Its own running row must not count as the in-flight poll, or every pass
        # after the first would decline and the runs would go unread.
        assert WorkerCommand.objects.filter(
            command="poll_security_rechecks", status="pending"
        ).exists()
        # One sleep, so the requeued row is not claimed instantly in a hot loop.
        assert mock_sleep.called

    @pytest.mark.django_db
    def test_a_finished_batch_is_not_re_queued(self) -> None:
        cmd = WorkerCommand.objects.create(command="poll_security_rechecks", status="running")
        with (
            patch(
                "franktheunicorn.security.recheck.poll_rechecks",
                return_value=(3, 1, 0),
            ),
            patch("franktheunicorn.worker.commands.time.sleep") as mock_sleep,
        ):
            _dispatch(cmd, make_operator_config())
        assert "3 finished, 1 failed" in cmd.log
        assert "re-queued" not in cmd.log
        assert not mock_sleep.called
        assert not WorkerCommand.objects.filter(
            command="poll_security_rechecks", status="pending"
        ).exists()

    @pytest.mark.django_db
    def test_nothing_launched_says_so(self) -> None:
        cmd = WorkerCommand.objects.create(command="poll_security_rechecks", status="running")
        with patch(
            "franktheunicorn.security.recheck.poll_rechecks",
            return_value=(0, 0, 0),
        ):
            _dispatch(cmd, make_operator_config())
        assert "nothing to wait on" in cmd.log


class TestGitSweepHandlers:
    """The two targetless git sweeps. One command, every project.

    Targetless because the expensive part — the fetch and the branch walk — is
    per project, and one command per report would pay for it several hundred
    times over.
    """

    @pytest.mark.django_db
    def test_the_branch_sweep_covers_every_project_and_logs_each(self) -> None:
        from franktheunicorn.security.branch_scan import BranchMatchRun
        from tests.factories import ProjectFactory, SecurityReportFactory

        first, second = ProjectFactory(), ProjectFactory()
        SecurityReportFactory(project=first)
        SecurityReportFactory(project=second)
        cmd = WorkerCommand.objects.create(command="match_security_branches", status="running")

        with patch(
            "franktheunicorn.security.branch_scan.match_fix_branches",
            side_effect=lambda p, _c: BranchMatchRun(project=p.full_name, applied=1),
        ) as matcher:
            _dispatch(cmd, make_operator_config())

        assert matcher.call_count == 2
        assert first.full_name in cmd.log
        assert second.full_name in cmd.log

    @pytest.mark.django_db
    def test_the_fixed_sweep_covers_every_project(self) -> None:
        from franktheunicorn.security.branch_scan import FixedScanRun
        from tests.factories import ProjectFactory, SecurityReportFactory

        project = ProjectFactory()
        SecurityReportFactory(project=project)
        cmd = WorkerCommand.objects.create(command="scan_security_fixed", status="running")

        with patch(
            "franktheunicorn.security.branch_scan.scan_already_fixed",
            side_effect=lambda p, _c: FixedScanRun(project=p.full_name, fixed=2),
        ) as scanner:
            _dispatch(cmd, make_operator_config())

        assert scanner.call_count == 1
        assert "2 likely fixed" in cmd.log

    @pytest.mark.django_db
    def test_one_project_raising_keeps_the_others_results(self) -> None:
        """The whole comprehension used to be evaluated before `cmd.log` was
        assigned, so project 57 of 60 raising lost the 56 that had finished. The
        sweeps promise they never raise; nothing enforces that, and a promise is
        not a reason to throw away an hour of git."""
        from franktheunicorn.security.branch_scan import BranchMatchRun
        from tests.factories import ProjectFactory, SecurityReportFactory

        good, bad = ProjectFactory(), ProjectFactory()
        SecurityReportFactory(project=good)
        SecurityReportFactory(project=bad)
        cmd = WorkerCommand.objects.create(command="match_security_branches", status="running")

        def _one_explodes(project: Any, _config: Any) -> BranchMatchRun:
            if project.pk == bad.pk:
                raise OSError("prepare_repo blew up")
            return BranchMatchRun(project=project.full_name, applied=1)

        with patch(
            "franktheunicorn.security.branch_scan.match_fix_branches", side_effect=_one_explodes
        ):
            _dispatch(cmd, make_operator_config())

        assert good.full_name in cmd.log
        assert "the sweep raised" in cmd.log

    @pytest.mark.django_db
    def test_an_empty_backlog_says_so_rather_than_looking_like_a_clean_sweep(self) -> None:
        """Zero projects and zero findings must not read the same."""
        cmd = WorkerCommand.objects.create(command="match_security_branches", status="running")

        with patch("franktheunicorn.security.branch_scan.match_fix_branches") as matcher:
            _dispatch(cmd, make_operator_config())

        assert not matcher.called
        assert "no project has an open security report" in cmd.log
