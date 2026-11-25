"""The Fork Setup worker commands and the per-cycle pass that queues the sweep."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import ForkSetupConfig, ProjectConfig
from franktheunicorn.core.models import WorkerCommand
from franktheunicorn.review.fork_setup import ScanResult
from franktheunicorn.worker.commands import _COMMAND_COST_TIER, process_pending_commands
from franktheunicorn.worker.runner import _run_fork_setup_pass
from tests.factories import PullRequestFactory

SPARK = ProjectConfig(owner="apache", repo="spark")


@pytest.mark.django_db
class TestPass:
    def test_queues_one_bulk_sweep_per_cycle(self) -> None:
        _run_fork_setup_pass([SPARK, "junk"])
        _run_fork_setup_pass([SPARK])
        [cmd] = WorkerCommand.objects.filter(command="scan_fork_setup")
        assert (cmd.priority, cmd.kwargs) == (0, {})

    def test_nothing_enabled_queues_nothing(self) -> None:
        off = ProjectConfig(owner="apache", repo="spark", fork_setup=ForkSetupConfig(enabled=False))
        _run_fork_setup_pass([off])
        assert not WorkerCommand.objects.exists()

    def test_a_queueing_failure_does_not_take_the_cycle_down(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch(
            "franktheunicorn.security.queue.queue_fork_setup_scan", side_effect=RuntimeError("db")
        ):
            _run_fork_setup_pass([SPARK])
        assert "Could not queue the fork setup sweep" in caplog.text


@pytest.mark.django_db
class TestCommands:
    def test_diagnose_dispatches(self) -> None:
        pr = PullRequestFactory()
        cmd = WorkerCommand.objects.create(command="diagnose_fork_setup", pull_request=pr)
        operator_config = MagicMock()
        with patch(
            "franktheunicorn.review.fork_setup.diagnose_pr", return_value="PR #1: no-run"
        ) as diagnose_pr:
            process_pending_commands(operator_config)
        cmd.refresh_from_db()
        assert (cmd.status, cmd.log) == ("completed", "PR #1: no-run")
        assert diagnose_pr.call_args[0] == (pr, operator_config)

    def test_diagnose_requires_a_pr(self) -> None:
        cmd = WorkerCommand.objects.create(command="diagnose_fork_setup")
        process_pending_commands(MagicMock())
        cmd.refresh_from_db()
        assert cmd.status == "failed"
        assert "requires a pull_request" in cmd.error

    @pytest.mark.parametrize("kwargs", [{}, {"every_project": True}])
    def test_scan_dispatches_with_the_scope(self, kwargs: dict[str, Any]) -> None:
        cmd = WorkerCommand.objects.create(command="scan_fork_setup", kwargs=kwargs)
        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[SPARK]),
            patch(
                "franktheunicorn.review.fork_setup.scan",
                return_value=[ScanResult(project="apache/spark", candidates=2)],
            ) as scan,
        ):
            process_pending_commands(MagicMock())
        cmd.refresh_from_db()
        assert cmd.status == "completed"
        assert "apache/spark: 2 candidate PR(s)" in cmd.log
        assert scan.call_args.kwargs == {"every_project": bool(kwargs)}

    def test_scan_with_nothing_to_sweep_says_why(self, caplog: pytest.LogCaptureFixture) -> None:
        cmd = WorkerCommand.objects.create(command="scan_fork_setup")
        with (
            caplog.at_level(logging.INFO),
            patch("franktheunicorn.review.fork_setup.scan", return_value=[]),
        ):
            process_pending_commands(MagicMock())
        cmd.refresh_from_db()
        assert "none sets fork_setup.enabled" in cmd.log

    def test_both_are_git_cheap(self) -> None:
        assert _COMMAND_COST_TIER["diagnose_fork_setup"] == 0
        assert _COMMAND_COST_TIER["scan_fork_setup"] == 0
