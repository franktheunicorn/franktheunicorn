"""Tests for alert mode — working-overlap and security-report alerts.

Covers config parsing, overlap detection, the dedup ledger, batched
email delivery (and its graceful degradation when no recipient is
configured), and the worker-cycle entry point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from franktheunicorn.alerts.service import (
    alert_email_recipient,
    find_working_overlap_reasons,
    render_alert_email_text,
    run_alert_sweep,
    send_pending_alert_emails,
    sweep_pr_alerts,
    sweep_security_report_alerts,
)
from franktheunicorn.config.models import (
    AlertsConfig,
    OperatorConfig,
    ProjectAlertsConfig,
    ProjectConfig,
)
from franktheunicorn.core.models import Alert
from tests.factories import (
    AlertFactory,
    ProjectFactory,
    PullRequestFactory,
    SecurityReportFactory,
)

if TYPE_CHECKING:
    from franktheunicorn.core.models import Project, PullRequest


def _operator_config(**alerts_kwargs: Any) -> OperatorConfig:
    alerts_kwargs.setdefault("enabled", True)
    return OperatorConfig(github_username="holdenk", alerts=AlertsConfig(**alerts_kwargs))


def _project_config(project: Project, **alerts_kwargs: Any) -> ProjectConfig:
    return ProjectConfig(
        owner=project.owner,
        repo=project.repo,
        alerts=ProjectAlertsConfig(**alerts_kwargs),
    )


class TestAlertsConfigParsing:
    def test_operator_defaults_disabled(self) -> None:
        config = OperatorConfig(github_username="holdenk")
        assert config.alerts.enabled is False
        assert config.alerts.email == ""
        assert config.alerts.security_reports is True

    def test_operator_yaml_round_trip(self) -> None:
        config = OperatorConfig(
            github_username="holdenk",
            alerts={"enabled": True, "email": "me@example.com", "security_reports": False},
        )
        assert config.alerts.enabled is True
        assert config.alerts.email == "me@example.com"
        assert config.alerts.security_reports is False

    def test_project_defaults_participate_once_master_switch_is_on(self) -> None:
        config = ProjectConfig(owner="apache", repo="spark")
        assert config.alerts.enabled is True
        assert config.alerts.working_overlap is True
        assert config.alerts.security_reports is True
        assert config.alerts.working_paths == []
        assert config.alerts.working_keywords == []

    def test_project_yaml_round_trip(self) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            alerts={
                "enabled": True,
                "working_paths": ["core/src/main/scala/org/apache/spark/storage/"],
                "working_keywords": ["decommission"],
            },
        )
        assert config.alerts.working_paths == ["core/src/main/scala/org/apache/spark/storage/"]
        assert config.alerts.working_keywords == ["decommission"]

    def test_spark_example_config_has_alerts_enabled(self) -> None:
        from pathlib import Path

        import yaml

        example = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "examples"
            / "projects"
            / "apache-spark.yaml"
        )
        config = ProjectConfig(**yaml.safe_load(example.read_text()))
        assert config.alerts.enabled is True
        assert config.alerts.working_overlap is True
        assert config.alerts.security_reports is True
        assert config.alerts.working_paths
        assert config.alerts.working_keywords


@pytest.mark.django_db
class TestFindWorkingOverlapReasons:
    def test_overlap_with_operator_open_pr(self, db_project: Project) -> None:
        PullRequestFactory(
            project=db_project,
            number=100,
            title="My shuffle refactor",
            is_operator_pr=True,
            state="open",
            changed_files=["core/shuffle.py", "core/util.py"],
        )
        pr = PullRequestFactory(
            project=db_project,
            number=101,
            changed_files=["core/shuffle.py", "docs/index.md"],
        )
        reasons = find_working_overlap_reasons(pr, _project_config(db_project))
        assert len(reasons) == 1
        assert "your open PR #100" in reasons[0]
        assert "core/shuffle.py" in reasons[0]
        assert "docs/index.md" not in reasons[0]

    def test_no_overlap_returns_empty(self, db_project: Project) -> None:
        PullRequestFactory(
            project=db_project,
            number=100,
            is_operator_pr=True,
            state="open",
            changed_files=["core/shuffle.py"],
        )
        pr = PullRequestFactory(project=db_project, number=101, changed_files=["docs/index.md"])
        assert find_working_overlap_reasons(pr, _project_config(db_project)) == []

    def test_closed_operator_pr_does_not_count(self, db_project: Project) -> None:
        PullRequestFactory(
            project=db_project,
            number=100,
            is_operator_pr=True,
            state="merged",
            changed_files=["core/shuffle.py"],
        )
        pr = PullRequestFactory(project=db_project, number=101, changed_files=["core/shuffle.py"])
        assert find_working_overlap_reasons(pr, _project_config(db_project)) == []

    def test_working_paths_prefix_and_glob(self, db_project: Project) -> None:
        pr = PullRequestFactory(
            project=db_project,
            number=101,
            changed_files=["sql/catalyst/Optimizer.scala", "python/pyspark/worker.py"],
        )
        pc = _project_config(db_project, working_paths=["sql/catalyst/", "python/**/worker.py"])
        reasons = find_working_overlap_reasons(pr, pc)
        assert len(reasons) == 1
        assert "working paths" in reasons[0]
        assert "sql/catalyst/Optimizer.scala" in reasons[0]
        assert "python/pyspark/worker.py" in reasons[0]

    def test_working_keywords_case_insensitive_title_and_body(self, db_project: Project) -> None:
        pr = PullRequestFactory(
            project=db_project,
            number=101,
            title="Improve executor Decommission handling",
            body="also mentions spark connect in the body",
            changed_files=["docs/index.md"],
        )
        pc = _project_config(
            db_project, working_keywords=["decommission", "spark connect", "unrelated"]
        )
        reasons = find_working_overlap_reasons(pr, pc)
        assert len(reasons) == 1
        assert "decommission" in reasons[0]
        assert "spark connect" in reasons[0]
        assert "unrelated" not in reasons[0]

    def test_file_list_is_capped(self, db_project: Project) -> None:
        files = [f"src/mod_{i}.py" for i in range(8)]
        pr = PullRequestFactory(project=db_project, number=101, changed_files=files)
        pc = _project_config(db_project, working_paths=["src/"])
        reasons = find_working_overlap_reasons(pr, pc)
        assert len(reasons) == 1
        assert "(+3 more)" in reasons[0]


@pytest.mark.django_db
class TestSweepPrAlerts:
    def _setup_overlap(self, project: Project) -> PullRequest:
        PullRequestFactory(
            project=project,
            number=100,
            is_operator_pr=True,
            state="open",
            changed_files=["core/shuffle.py"],
        )
        return PullRequestFactory(
            project=project,
            number=101,
            author="someone-else",
            changed_files=["core/shuffle.py"],
        )

    def test_creates_alert_once(self, db_project: Project) -> None:
        pr = self._setup_overlap(db_project)
        configs = [_project_config(db_project)]
        operator = _operator_config()

        created = sweep_pr_alerts(configs, operator)
        assert len(created) == 1
        alert = created[0]
        assert alert.alert_type == "working-overlap"
        assert alert.pull_request == pr
        assert alert.project == db_project
        assert alert.dedup_key == f"working-overlap:pr:{pr.pk}"
        assert alert.reasons

        # Second sweep is a no-op thanks to the dedup ledger.
        assert sweep_pr_alerts(configs, operator) == []
        assert Alert.objects.count() == 1

    def test_operator_own_pr_never_alerts(self, db_project: Project) -> None:
        PullRequestFactory(
            project=db_project,
            number=100,
            is_operator_pr=True,
            state="open",
            changed_files=["core/shuffle.py"],
        )
        PullRequestFactory(
            project=db_project,
            number=102,
            is_operator_pr=True,
            state="open",
            changed_files=["core/shuffle.py"],
        )
        assert sweep_pr_alerts([_project_config(db_project)], _operator_config()) == []

    def test_closed_pr_does_not_alert(self, db_project: Project) -> None:
        pr = self._setup_overlap(db_project)
        pr.state = "closed"
        pr.save(update_fields=["state"])
        assert sweep_pr_alerts([_project_config(db_project)], _operator_config()) == []

    def test_gates(self, db_project: Project) -> None:
        self._setup_overlap(db_project)

        # Operator master switch off.
        assert sweep_pr_alerts([_project_config(db_project)], _operator_config(enabled=False)) == []

        # Project alerts disabled.
        assert (
            sweep_pr_alerts([_project_config(db_project, enabled=False)], _operator_config()) == []
        )

        # Working-overlap alerts disabled for the project.
        assert (
            sweep_pr_alerts(
                [_project_config(db_project, working_overlap=False)], _operator_config()
            )
            == []
        )

        # Disabled project config is skipped entirely.
        pc = _project_config(db_project)
        pc.enabled = False
        assert sweep_pr_alerts([pc], _operator_config()) == []


@pytest.mark.django_db
class TestSweepSecurityReportAlerts:
    def test_alerts_for_new_and_triaging(self, db_project: Project) -> None:
        new_report = SecurityReportFactory(project=db_project, status="new")
        triaging_report = SecurityReportFactory(project=db_project, status="triaging")
        SecurityReportFactory(project=db_project, status="valid")
        SecurityReportFactory(project=db_project, status="invalid")

        created = sweep_security_report_alerts([_project_config(db_project)], _operator_config())
        assert {a.security_report for a in created} == {new_report, triaging_report}
        assert all(a.alert_type == "security-report" for a in created)
        by_report = {a.security_report: a for a in created}
        assert "in the queue" in by_report[new_report].title
        assert "in triage" in by_report[triaging_report].title

    def test_status_change_does_not_realert(self, db_project: Project) -> None:
        report = SecurityReportFactory(project=db_project, status="new")
        configs = [_project_config(db_project)]
        assert len(sweep_security_report_alerts(configs, _operator_config())) == 1

        report.status = "triaging"
        report.save(update_fields=["status"])
        assert sweep_security_report_alerts(configs, _operator_config()) == []
        assert Alert.objects.count() == 1

    def test_report_without_project_alerts(self) -> None:
        report = SecurityReportFactory(project=None, status="new", title="")
        created = sweep_security_report_alerts([], _operator_config())
        assert len(created) == 1
        assert created[0].security_report == report
        # Falls back to raw_text when the report has no title yet.
        assert report.raw_text[:40] in created[0].title

    def test_multiline_pasted_report_gets_single_line_title(self) -> None:
        # A title with CR/LF would later blow up the email Subject header
        # (Django BadHeaderError), stranding the alert unsent forever.
        SecurityReportFactory(
            project=None,
            status="new",
            title="",
            raw_text="SQL injection in login form\r\nSteps to reproduce:\n1. do the thing",
        )
        created = sweep_security_report_alerts([], _operator_config())
        assert len(created) == 1
        assert "\n" not in created[0].title
        assert "\r" not in created[0].title
        assert "SQL injection in login form Steps to reproduce:" in created[0].title

    def test_project_opt_out(self, db_project: Project) -> None:
        SecurityReportFactory(project=db_project, status="new")
        configs = [_project_config(db_project, security_reports=False)]
        assert sweep_security_report_alerts(configs, _operator_config()) == []

        # A report on a project *without* a config is governed by the
        # operator-level toggle alone.
        other = ProjectFactory(owner="other", repo="repo")
        SecurityReportFactory(project=other, status="new")
        assert len(sweep_security_report_alerts(configs, _operator_config())) == 1

    def test_operator_toggle_off(self, db_project: Project) -> None:
        SecurityReportFactory(project=db_project, status="new")
        operator = _operator_config(security_reports=False)
        assert sweep_security_report_alerts([_project_config(db_project)], operator) == []

    def test_disabled_project_config_suppresses_report_alerts(self, db_project: Project) -> None:
        # A project disabled outright (enabled: false) is silent here just
        # like everywhere else in the worker — not treated as unconfigured.
        SecurityReportFactory(project=db_project, status="new")
        pc = _project_config(db_project)
        pc.enabled = False
        assert sweep_security_report_alerts([pc], _operator_config()) == []
        assert Alert.objects.count() == 0


@pytest.mark.django_db
class TestAlertEmail:
    def test_recipient_falls_back_to_digest_email(self) -> None:
        operator = OperatorConfig(
            github_username="holdenk",
            digest_email="digest@example.com",
            alerts=AlertsConfig(enabled=True),
        )
        assert alert_email_recipient(operator) == "digest@example.com"

        operator.alerts.email = "alerts@example.com"
        assert alert_email_recipient(operator) == "alerts@example.com"

    def test_multiline_title_still_sends_single_alert_email(self, mailoutbox: list[Any]) -> None:
        # Even if a multi-line title reaches the DB (pre-sanitizer rows,
        # future alert types), the subject must be flattened — a newline
        # there raises BadHeaderError and the alert would retry forever.
        AlertFactory(title="Security report in the queue: line one\nline two")
        sent = send_pending_alert_emails(_operator_config(email="me@example.com"))
        assert sent == 1
        assert len(mailoutbox) == 1
        expected = "[frank alert] Security report in the queue: line one line two"
        assert mailoutbox[0].subject == expected
        assert Alert.objects.get().email_sent is True

    def test_sends_single_alert_email(self, mailoutbox: list[Any]) -> None:
        AlertFactory(title="apache/spark#1 overlaps", reasons=["touches core/shuffle.py"])
        sent = send_pending_alert_emails(_operator_config(email="me@example.com"))
        assert sent == 1
        assert len(mailoutbox) == 1
        assert mailoutbox[0].subject == "[frank alert] apache/spark#1 overlaps"
        assert mailoutbox[0].to == ["me@example.com"]
        assert "touches core/shuffle.py" in mailoutbox[0].body

        alert = Alert.objects.get()
        assert alert.email_sent is True
        assert alert.emailed_at is not None

    def test_batches_multiple_alerts_into_one_email(self, mailoutbox: list[Any]) -> None:
        AlertFactory(title="first overlap")
        AlertFactory(
            alert_type="security-report",
            dedup_key="security-report:report:1",
            title="Security report in the queue: XSS",
        )
        sent = send_pending_alert_emails(_operator_config(email="me@example.com"))
        assert sent == 2
        assert len(mailoutbox) == 1
        assert mailoutbox[0].subject == "[frank alert] 2 new alerts"
        assert "first overlap" in mailoutbox[0].body
        assert "XSS" in mailoutbox[0].body

        # Nothing left to send afterwards.
        assert send_pending_alert_emails(_operator_config(email="me@example.com")) == 0
        assert len(mailoutbox) == 1

    def test_no_recipient_records_but_does_not_send(self, mailoutbox: list[Any]) -> None:
        AlertFactory(title="unsent alert")
        sent = send_pending_alert_emails(_operator_config(email=""))
        assert sent == 0
        assert mailoutbox == []
        assert Alert.objects.get().email_sent is False

    def test_send_failure_keeps_alerts_pending(self, mailoutbox: list[Any]) -> None:
        AlertFactory(title="flaky smtp")
        with patch("django.core.mail.send_mail", side_effect=OSError("smtp down")):
            sent = send_pending_alert_emails(_operator_config(email="me@example.com"))
        assert sent == 0
        assert Alert.objects.get().email_sent is False
        # Next attempt succeeds and delivers the same alert.
        assert send_pending_alert_emails(_operator_config(email="me@example.com")) == 1
        assert len(mailoutbox) == 1

    def test_email_body_includes_pr_url(self, db_pr: PullRequest, mailoutbox: list[Any]) -> None:
        alert = AlertFactory(
            title="overlap",
            pull_request=db_pr,
            project=db_pr.project,
            reasons=["touches files"],
        )
        body = render_alert_email_text([alert])
        assert db_pr.url in body
        assert "[Working Overlap]" in body


@pytest.mark.django_db
class TestRunAlertSweep:
    def test_end_to_end(self, db_project: Project, mailoutbox: list[Any]) -> None:
        PullRequestFactory(
            project=db_project,
            number=100,
            is_operator_pr=True,
            state="open",
            changed_files=["core/shuffle.py"],
        )
        PullRequestFactory(
            project=db_project,
            number=101,
            author="someone-else",
            changed_files=["core/shuffle.py"],
        )
        SecurityReportFactory(project=db_project, status="new", title="Path traversal")

        run_alert_sweep([_project_config(db_project)], _operator_config(email="me@example.com"))

        assert Alert.objects.count() == 2
        assert len(mailoutbox) == 1
        assert mailoutbox[0].subject == "[frank alert] 2 new alerts"
        assert "Path traversal" in mailoutbox[0].body
        assert not Alert.objects.filter(email_sent=False).exists()

    def test_disabled_or_missing_config_is_inert(self, db_project: Project) -> None:
        SecurityReportFactory(project=db_project, status="new")
        run_alert_sweep([_project_config(db_project)], None)
        run_alert_sweep([_project_config(db_project)], _operator_config(enabled=False))
        assert Alert.objects.count() == 0

    def test_never_raises(self) -> None:
        with patch(
            "franktheunicorn.alerts.service.sweep_pr_alerts",
            side_effect=RuntimeError("boom"),
        ):
            run_alert_sweep([], _operator_config())


@pytest.mark.django_db
class TestWorkerCycleIntegration:
    def test_run_cycle_invokes_alert_sweep(self, operator_config: OperatorConfig) -> None:
        from franktheunicorn.worker import runner

        with (
            patch("franktheunicorn.alerts.service.run_alert_sweep") as mock_sweep,
            patch.object(runner, "_fetch_dependency_changelogs_for_cycle"),
            patch.object(runner, "_run_shepherding_pass"),
            patch.object(runner, "_poll_security_emails"),
            patch.object(runner, "_scan_mentioned_prs"),
            patch.object(runner, "_backfill_unreviewed_prs"),
        ):
            runner._run_cycle({}, [], "holdenk", operator_config)

        mock_sweep.assert_called_once_with([], operator_config)

    def test_run_cycle_skips_sweep_without_operator_config(self) -> None:
        from franktheunicorn.worker import runner

        with (
            patch("franktheunicorn.alerts.service.run_alert_sweep") as mock_sweep,
            patch.object(runner, "_fetch_dependency_changelogs_for_cycle"),
            patch.object(runner, "_run_shepherding_pass"),
            patch.object(runner, "_poll_security_emails"),
            patch.object(runner, "_scan_mentioned_prs"),
            patch.object(runner, "_backfill_unreviewed_prs"),
        ):
            runner._run_cycle({}, [], "holdenk", None)

        mock_sweep.assert_not_called()
