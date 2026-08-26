"""Tests for the single door onto security-triage queueing."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig
from franktheunicorn.core.models import WorkerCommand
from franktheunicorn.security.queue import (
    queue_triage,
    queue_triage_if_enabled,
    queue_triage_on_request,
)
from tests.factories import SecurityReportFactory


def _config(*, enabled: bool = True, auto_triage: bool = True) -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        security_triage=SecurityTriageConfig(enabled=enabled, auto_triage=auto_triage),
    )


@pytest.mark.django_db
class TestQueueTriage:
    def test_queues_once(self) -> None:
        report = SecurityReportFactory()

        assert queue_triage(report) is True
        assert WorkerCommand.objects.filter(security_report=report).count() == 1

    def test_second_call_while_in_flight_is_a_no_op(self) -> None:
        """Each run costs an NVD lookup plus two LLM calls; a double-click must not buy two."""
        report = SecurityReportFactory()
        queue_triage(report)

        assert queue_triage(report) is False
        assert WorkerCommand.objects.filter(security_report=report).count() == 1

    def test_requeues_after_the_previous_run_finished(self) -> None:
        report = SecurityReportFactory()
        queue_triage(report)
        WorkerCommand.objects.filter(security_report=report).update(status="completed")

        assert queue_triage(report) is True
        assert WorkerCommand.objects.filter(security_report=report).count() == 2

    def test_the_db_constraint_is_the_whole_guarantee(self) -> None:
        """There's no pre-flight SELECT any more — the partial unique index decides.

        It used to be checked first and the constraint was the backstop, which
        meant a bulk import ran one provably-empty SELECT per freshly created row.
        Dropping it changes nothing an observer can see: a second call still
        returns False and still leaves one command.
        """
        report = SecurityReportFactory()

        assert queue_triage(report) is True
        assert queue_triage(report) is False
        assert WorkerCommand.objects.filter(security_report=report).count() == 1

    def test_a_finished_run_does_not_block_a_new_one(self) -> None:
        """The index is partial — only pending/running conflict."""
        report = SecurityReportFactory()
        queue_triage(report)
        WorkerCommand.objects.filter(security_report=report).update(status="completed")

        assert queue_triage(report) is True
        assert WorkerCommand.objects.filter(security_report=report).count() == 2


@pytest.mark.django_db
class TestQueueTriageIfEnabled:
    def test_queues_when_both_settings_are_on(self) -> None:
        report = SecurityReportFactory()

        assert queue_triage_if_enabled(report, _config()) is True
        assert WorkerCommand.objects.filter(security_report=report).exists()

    def test_auto_triage_off_queues_nothing(self) -> None:
        report = SecurityReportFactory()

        assert queue_triage_if_enabled(report, _config(auto_triage=False)) is False
        assert not WorkerCommand.objects.exists()

    def test_feature_disabled_queues_nothing_even_with_auto_triage_on(self) -> None:
        report = SecurityReportFactory()

        assert queue_triage_if_enabled(report, _config(enabled=False)) is False
        assert not WorkerCommand.objects.exists()

    def test_loads_the_operator_config_when_not_given_one(self) -> None:
        report = SecurityReportFactory()

        with patch("franktheunicorn.config.loader.get_operator_config", return_value=_config()):
            assert queue_triage_if_enabled(report) is True

        assert WorkerCommand.objects.filter(security_report=report).exists()


@pytest.mark.django_db
class TestQueueTriageOnRequest:
    """The explicit-ask door. Ungated on purpose, which is worth pinning down.

    Nothing downstream re-checks security_triage — not the worker dispatcher, not
    triage_report — so these assertions are the whole contract.
    """

    def test_neither_setting_vetoes_an_explicit_request(self) -> None:
        report = SecurityReportFactory()

        assert queue_triage_on_request(report, _config(enabled=False, auto_triage=False)) is True
        assert WorkerCommand.objects.filter(security_report=report).count() == 1

    def test_it_works_without_a_config_at_all(self) -> None:
        """No config is read, so a broken operator.yaml can't break the button."""
        report = SecurityReportFactory()

        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            side_effect=AssertionError("must not be loaded"),
        ):
            assert queue_triage_on_request(report) is True

    def test_it_still_goes_through_the_one_door(self) -> None:
        """Ungated, not unguarded — the in-flight check still applies."""
        report = SecurityReportFactory()

        assert queue_triage_on_request(report) is True
        assert queue_triage_on_request(report) is False
        assert WorkerCommand.objects.filter(security_report=report).count() == 1
