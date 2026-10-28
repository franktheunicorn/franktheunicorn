"""Tests for the worker's introduced-CVE scan scheduler.

The scheduler is a read-only tick: for each project with the feature on and a
cadence that has elapsed, it queues one bulk-priority ``scan_introduced_cves``
command. A project whose cadence hasn't elapsed, or whose feature is off, is
skipped silently — the scan itself is the expensive part and goes through the
worker queue.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import (
    IntroducedScanConfig,
    OperatorConfig,
    ProjectConfig,
    SecurityTriageConfig,
)
from franktheunicorn.core.models import IntroducedScanRun, WorkerCommand
from franktheunicorn.worker import runner


def _operator_config() -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        security_triage=SecurityTriageConfig(enabled=True),
    )


def _project_config(scan: IntroducedScanConfig) -> ProjectConfig:
    return ProjectConfig(
        owner="apache",
        repo="spark",
        review_context="spark",
        introduced_scan=scan,
    )


@pytest.mark.django_db
class TestScheduleIntroducedScans:
    def test_a_due_project_queues_a_scan(self) -> None:
        scan = IntroducedScanConfig(enabled=True, cadence="daily")
        pc = _project_config(scan)
        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch("franktheunicorn.security.introduced_scan.is_due", return_value=True),
        ):
            runner._schedule_introduced_scans(_operator_config())
        assert WorkerCommand.objects.filter(command="scan_introduced_cves").count() == 1

    def test_a_not_due_project_is_skipped(self) -> None:
        scan = IntroducedScanConfig(enabled=True, cadence="daily")
        pc = _project_config(scan)
        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch("franktheunicorn.security.introduced_scan.is_due", return_value=False),
        ):
            runner._schedule_introduced_scans(_operator_config())
        assert not WorkerCommand.objects.filter(command="scan_introduced_cves").exists()

    def test_a_disabled_project_is_skipped(self) -> None:
        scan = IntroducedScanConfig(enabled=False, cadence="daily")
        pc = _project_config(scan)
        with patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]):
            runner._schedule_introduced_scans(_operator_config())
        assert not WorkerCommand.objects.filter(command="scan_introduced_cves").exists()

    def test_a_recent_run_blocks_a_requeue(self) -> None:
        """A run started inside the cadence window means is_due returns False, no queue."""
        from django.utils import timezone

        from tests.factories import ProjectFactory

        project = ProjectFactory(owner="apache", repo="spark")
        IntroducedScanRun.objects.create(
            project=project,
            started_at=timezone.now() - timedelta(hours=1),
            finished_at=timezone.now() - timedelta(minutes=55),
            cadence="daily",
            commit_range="last 2 day(s)",
        )
        scan = IntroducedScanConfig(enabled=True, cadence="daily")
        pc = _project_config(scan)
        with patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]):
            runner._schedule_introduced_scans(_operator_config())
        assert not WorkerCommand.objects.filter(command="scan_introduced_cves").exists()

    def test_a_loader_error_does_not_crash_the_cycle(self) -> None:
        """A broken project config must not take the whole scheduler down with it."""
        with patch(
            "franktheunicorn.config.loader.load_project_configs",
            side_effect=RuntimeError("boom"),
        ):
            runner._schedule_introduced_scans(_operator_config())
        assert not WorkerCommand.objects.filter(command="scan_introduced_cves").exists()
