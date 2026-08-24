"""Tests for security report dashboard views."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client

from franktheunicorn.core.models import SecurityReport, SecurityTriageFeedback
from tests.factories import (
    EmailScanRecordFactory,
    ProjectFactory,
    SecurityReportFactory,
    SecurityTriageGuidanceFactory,
)


@pytest.mark.django_db
class TestEmailActivityView:
    def test_renders_read_only_banner(self, client: Client) -> None:
        response = client.get("/security/email-activity/")
        assert response.status_code == 200
        assert b"Read-only" in response.content
        assert b"never" in response.content.lower()

    def test_shows_scanned_messages_and_keywords(self, client: Client) -> None:
        report = SecurityReportFactory(title="Path traversal")
        EmailScanRecordFactory(
            message_id="<a>",
            subject="[SECURITY] Path traversal via core_model_path",
            from_name="Ryan Hughes",
            from_email="security@apache.org",
            is_forwarded=True,
            matched_keywords=["path traversal", "vulnerability"],
            classified_security=True,
            action="ingested",
            security_report=report,
        )
        EmailScanRecordFactory(
            message_id="<b>",
            subject="Lunch plans",
            from_email="friend@example.com",
            matched_keywords=[],
            classified_security=False,
            action="skipped_not_security",
        )
        response = client.get("/security/email-activity/")
        body = response.content.decode()
        assert "Ryan Hughes" in body
        assert "path traversal" in body  # matched keyword chip
        assert "forwarded" in body
        assert "Lunch plans" in body  # non-security still shown for transparency
        assert ">2</strong> examined" in body  # both messages counted


@pytest.mark.django_db
class TestSecurityReportList:
    def test_list_view_renders(self, client: Client) -> None:
        response = client.get("/security/")
        assert response.status_code == 200
        assert b"Security Report Triage" in response.content

    def test_list_shows_reports(self, client: Client, db: Any) -> None:
        SecurityReportFactory(title="Test XSS vulnerability")
        response = client.get("/security/")
        assert response.status_code == 200
        assert b"Test XSS vulnerability" in response.content

    def test_list_filters_by_status(self, client: Client, db: Any) -> None:
        SecurityReportFactory(title="New report", status="new")
        SecurityReportFactory(title="Valid report", status="valid")

        response = client.get("/security/?status=valid")
        assert response.status_code == 200
        assert b"Valid report" in response.content
        assert b"New report" not in response.content

    def test_list_all_status_shows_everything(self, client: Client, db: Any) -> None:
        SecurityReportFactory(title="Report A", status="new")
        SecurityReportFactory(title="Report B", status="invalid")

        response = client.get("/security/")
        assert response.status_code == 200
        assert b"Report A" in response.content
        assert b"Report B" in response.content


@pytest.mark.django_db
class TestSecurityReportCreate:
    def test_create_form_renders(self, client: Client) -> None:
        response = client.get("/security/new/")
        assert response.status_code == 200
        assert b"Submit Security Report" in response.content

    def test_create_report_via_post(self, client: Client, db: Any) -> None:
        project = ProjectFactory()
        response = client.post(
            "/security/new/",
            {
                "raw_text": "SQL injection in /api/users endpoint",
                "title": "SQLi in users API",
                "project_id": project.pk,
                "reporter_name": "Alice",
                "reporter_email": "alice@test.com",
            },
        )
        # Should redirect to detail page.
        assert response.status_code == 302

        report = SecurityReport.objects.get()
        assert report.title == "SQLi in users API"
        assert report.raw_text == "SQL injection in /api/users endpoint"
        assert report.reporter_name == "Alice"
        assert report.source == "paste"
        assert report.project == project

    def test_create_without_project(self, client: Client, db: Any) -> None:
        response = client.post(
            "/security/new/",
            {"raw_text": "Some vulnerability report"},
        )
        assert response.status_code == 302
        report = SecurityReport.objects.get()
        assert report.project is None

    def test_create_requires_raw_text(self, client: Client, db: Any) -> None:
        response = client.post("/security/new/", {"raw_text": ""})
        assert response.status_code == 400

    def test_paste_forwarded_report_autofills_metadata(self, client: Client, db: Any) -> None:
        """Pasting a forwarded Apache report with blank fields recovers the
        reporter/title from the forwarded block — no LLM backend required."""
        forwarded = (
            "Dear PMC,\n\n"
            "The security vulnerability report has been received by the Apache\n"
            "Security Team and is being passed to you for action.\n\n"
            "---------- Forwarded message ---------\n"
            "From: Ryan Hughes via security <security@apache.org>\n"
            "Subject: [SECURITY] Path traversal via core_model_path\n"
            "To: <security@spark.apache.org>\n\n"
            "Hello, I am reporting a path traversal vulnerability exploit.\n"
        )
        response = client.post("/security/new/", {"raw_text": forwarded})
        assert response.status_code == 302

        report = SecurityReport.objects.get()
        assert report.reporter_name == "Ryan Hughes"  # "via security" stripped
        assert report.reporter_email == "security@apache.org"
        assert report.title.startswith("[SECURITY] Path traversal")
        # Full pasted text preserved for triage.
        assert "received by the Apache" in report.raw_text

    def test_operator_fields_win_over_autofill(self, client: Client, db: Any) -> None:
        """Anything the operator typed takes precedence over recovery."""
        forwarded = (
            "---------- Forwarded message ---------\n"
            "From: Ryan Hughes via security <security@apache.org>\n"
            "Subject: [SECURITY] Path traversal via core_model_path\n\n"
            "vulnerability exploit report body\n"
        )
        response = client.post(
            "/security/new/",
            {
                "raw_text": forwarded,
                "title": "My own title",
                "reporter_name": "Someone Else",
            },
        )
        assert response.status_code == 302
        report = SecurityReport.objects.get()
        assert report.title == "My own title"
        assert report.reporter_name == "Someone Else"
        # Email wasn't provided, so it still auto-fills.
        assert report.reporter_email == "security@apache.org"


@pytest.mark.django_db
class TestSecurityReportDetail:
    def test_detail_renders(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(
            title="Buffer overflow",
            raw_text="Overflow in parse_input()",
        )
        response = client.get(f"/security/{report.pk}/")
        assert response.status_code == 200
        assert b"Buffer overflow" in response.content
        assert b"Overflow in parse_input()" in response.content

    def test_detail_shows_triage_results(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(
            title="Expected behavior report",
            triage_summary="This is documented behavior.",
            is_expected_behavior=True,
            expected_behavior_explanation="The tool runs shell commands by design.",
            poc_plausible=False,
        )
        response = client.get(f"/security/{report.pk}/")
        assert response.status_code == 200
        assert b"Expected Behavior" in response.content

    def test_detail_shows_cve_matches(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(
            title="Known vuln",
            cve_matches=[
                {
                    "cve_id": "CVE-2024-1234",
                    "description": "Known issue",
                    "cvss_score": 7.5,
                    "status": "Analyzed",
                }
            ],
        )
        response = client.get(f"/security/{report.pk}/")
        assert response.status_code == 200
        assert b"CVE-2024-1234" in response.content

    def test_detail_404_for_missing(self, client: Client, db: Any) -> None:
        response = client.get("/security/99999/")
        assert response.status_code == 404


@pytest.mark.django_db
class TestSecurityReportVerdict:
    def test_set_verdict(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(status="new")
        response = client.post(
            f"/security/{report.pk}/verdict/",
            {
                "status": "invalid",
                "operator_notes": "This is not a real vulnerability.",
            },
        )
        assert response.status_code == 200
        report.refresh_from_db()
        assert report.status == "invalid"
        assert report.operator_notes == "This is not a real vulnerability."

    def test_set_duplicate_with_cve(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(status="new")
        response = client.post(
            f"/security/{report.pk}/verdict/",
            {
                "status": "duplicate",
                "matched_cve_id": "CVE-2024-5678",
                "operator_notes": "Duplicate of known issue.",
            },
        )
        assert response.status_code == 200
        report.refresh_from_db()
        assert report.status == "duplicate"
        assert report.matched_cve_id == "CVE-2024-5678"

    def test_invalid_status_rejected(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory()
        response = client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "not-a-real-status"},
        )
        assert response.status_code == 400

    def test_clearing_duplicate_clears_cve_id(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(status="duplicate", matched_cve_id="CVE-2024-1111")
        response = client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "operator_notes": "Actually valid."},
        )
        assert response.status_code == 200
        report.refresh_from_db()
        assert report.matched_cve_id == ""


@pytest.mark.django_db
class TestSecurityReportTriage:
    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_triage_endpoint_queues_worker_command(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import (
            LLMBackendConfig,
            OperatorConfig,
            SecurityTriageConfig,
        )
        from franktheunicorn.core.models import WorkerCommand

        # security_triage.enabled is required now: the manual button used to be
        # the one door that queued triage with the whole feature switched off.
        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )
        report = SecurityReportFactory(title="Test triage")

        response = client.post(f"/security/{report.pk}/triage/")
        assert response.status_code == 200
        assert b"Triage queued" in response.content
        cmd = WorkerCommand.objects.filter(
            command="run_security_triage", security_report=report
        ).first()
        assert cmd is not None

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_triage_no_backend_returns_error(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import OperatorConfig

        mock_config.return_value = OperatorConfig(github_username="testuser")
        report = SecurityReportFactory()

        response = client.post(f"/security/{report.pk}/triage/")
        assert response.status_code == 200
        assert b"No LLM backend configured" in response.content

    @patch("franktheunicorn.core.models.WorkerCommand.objects")
    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_triage_queue_failure_returns_error_html(
        self, mock_config: MagicMock, mock_objects: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import (
            LLMBackendConfig,
            OperatorConfig,
            SecurityTriageConfig,
        )

        # security_triage.enabled is required now: the manual button used to be
        # the one door that queued triage with the whole feature switched off.
        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )
        # Nothing in flight, so the enqueue is attempted — and fails.
        mock_objects.filter.return_value.exists.return_value = False
        mock_objects.create.side_effect = RuntimeError("db error")
        report = SecurityReportFactory()

        response = client.post(f"/security/{report.pk}/triage/")
        assert response.status_code == 200
        assert b"Failed to queue triage" in response.content

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_triage_does_not_queue_twice(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """Auto-triage plus a click (or a double-click) is one run, not two.

        Two runs mean two NVD lookups and two pairs of LLM calls, with the
        second overwriting the first's verdict.
        """
        from franktheunicorn.config.models import (
            LLMBackendConfig,
            OperatorConfig,
            SecurityTriageConfig,
        )
        from franktheunicorn.core.models import WorkerCommand

        # security_triage.enabled is required now: the manual button used to be
        # the one door that queued triage with the whole feature switched off.
        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )
        report = SecurityReportFactory()

        first = client.post(f"/security/{report.pk}/triage/")
        second = client.post(f"/security/{report.pk}/triage/")

        assert b"Triage queued" in first.content
        assert b"already queued" in second.content
        assert (
            WorkerCommand.objects.filter(
                command="run_security_triage", security_report=report
            ).count()
            == 1
        )

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_triage_can_be_requeued_after_a_finished_run(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import (
            LLMBackendConfig,
            OperatorConfig,
            SecurityTriageConfig,
        )
        from franktheunicorn.core.models import WorkerCommand

        # security_triage.enabled is required now: the manual button used to be
        # the one door that queued triage with the whole feature switched off.
        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )
        report = SecurityReportFactory()
        client.post(f"/security/{report.pk}/triage/")
        WorkerCommand.objects.filter(security_report=report).update(status="failed")

        response = client.post(f"/security/{report.pk}/triage/")

        assert b"Triage queued" in response.content
        assert WorkerCommand.objects.filter(security_report=report).count() == 2

    def test_failed_triage_is_visible_on_the_report_page(self, client: Client, db: Any) -> None:
        """A worker-side failure must not look like a run that never happened."""
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(triage_summary="")
        cmd = WorkerCommand.objects.create(command="run_security_triage", security_report=report)
        WorkerCommand.objects.filter(pk=cmd.pk).update(
            status="failed", error="RuntimeError: model timed out"
        )

        response = client.get(f"/security/{report.pk}/")

        assert b"Triage failed in the worker" in response.content
        assert b"model timed out" in response.content
        assert b"Re-run LLM Triage" in response.content

    def test_completed_run_with_no_assessment_still_offers_a_retry(
        self, client: Client, db: Any
    ) -> None:
        """The blank-panel dead end: triage_summary is optional in the model's answer.

        A completed command with an empty summary matched no status strip and no
        result panel, so the page came back empty with no button to re-run.
        """
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(triage_summary="", poc_assessment="", poc_plausible=None)
        cmd = WorkerCommand.objects.create(command="run_security_triage", security_report=report)
        WorkerCommand.objects.filter(pk=cmd.pk).update(status="completed")

        response = client.get(f"/security/{report.pk}/")

        assert b"produced no assessment" in response.content
        assert b"Re-run LLM Triage" in response.content

    def test_a_poc_verdict_without_a_summary_still_renders(self, client: Client, db: Any) -> None:
        """The result panel shows POC fields, so gating it on the summary hid them."""
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(
            triage_summary="", poc_plausible=True, poc_assessment="The traversal reproduces."
        )
        cmd = WorkerCommand.objects.create(command="run_security_triage", security_report=report)
        WorkerCommand.objects.filter(pk=cmd.pk).update(status="completed")

        response = client.get(f"/security/{report.pk}/")

        assert b"Triage Analysis" in response.content
        assert b"The traversal reproduces." in response.content
        assert b"produced no assessment" not in response.content

    def test_an_in_flight_run_hides_the_button(self, client: Client, db: Any) -> None:
        """Nothing to click while the worker is mid-run; the constraint would reject it."""
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(triage_summary="")
        WorkerCommand.objects.create(command="run_security_triage", security_report=report)

        response = client.get(f"/security/{report.pk}/")

        assert b"Triage pending" in response.content
        # Asserted on the hx-post attribute, not on "LLM Triage</button>": the
        # rendered bytes are "LLM Triage\n    </button>", so the old spelling
        # could never match and the test could not fail.
        assert b"security/%d/triage/" % report.pk not in response.content

    def test_a_severity_only_run_is_not_called_empty(self, client: Client, db: Any) -> None:
        """The badge the run wrote sat directly above "produced no assessment"."""
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(
            triage_summary="", poc_assessment="", poc_plausible=None, assessed_severity="high"
        )
        cmd = WorkerCommand.objects.create(command="run_security_triage", security_report=report)
        WorkerCommand.objects.filter(pk=cmd.pk).update(status="completed")

        response = client.get(f"/security/{report.pk}/")

        assert b"produced no assessment" not in response.content

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_the_triage_button_works_without_the_feature_flag(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """security_triage.enabled defaults False and ships commented out.

        Gating an explicit click on it made the button a permanent no-op on the
        install the documented setup produces.
        """
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory()

        response = client.post(f"/security/{report.pk}/triage/")

        assert b"Triage queued" in response.content
        assert WorkerCommand.objects.filter(security_report=report).count() == 1

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_queue_failure_still_renders_the_panel_not_a_500(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """The recovery path re-queries the table whose failure got us here."""
        from django.db import OperationalError

        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory()

        with (
            patch(
                "franktheunicorn.security.queue.queue_triage_on_request",
                side_effect=OperationalError("database is locked"),
            ),
            patch(
                "franktheunicorn.core.models.WorkerCommand.objects.filter",
                side_effect=OperationalError("database is locked"),
            ),
        ):
            response = client.post(f"/security/{report.pk}/triage/")

        # htmx does not swap on 5xx, so a 500 here means the click does nothing.
        assert response.status_code == 200
        assert b"Failed to queue triage" in response.content

    def test_an_untriaged_report_offers_the_first_run(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(triage_summary="")

        response = client.get(f"/security/{report.pk}/")

        assert b"Run LLM Triage" in response.content
        assert b"Re-run LLM Triage" not in response.content


@pytest.mark.django_db
class TestSecurityReportCveCheck:
    @patch("franktheunicorn.security.cve_lookup.search_cves")
    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_cve_check_endpoint(
        self, mock_config: MagicMock, mock_cves: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig
        from franktheunicorn.security.cve_lookup import CVEMatch

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True),
        )
        mock_cves.return_value = [
            CVEMatch(cve_id="CVE-2024-9999", description="Test", cvss_score=5.0)
        ]

        report = SecurityReportFactory(parsed_component="parser.c")
        response = client.post(f"/security/{report.pk}/cve-check/")
        assert response.status_code == 200
        report.refresh_from_db()
        assert len(report.cve_matches) == 1

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_cve_check_no_keyword(self, mock_config: MagicMock, client: Client, db: Any) -> None:
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True),
        )
        report = SecurityReportFactory(title="", parsed_component="")
        response = client.post(f"/security/{report.pk}/cve-check/")
        assert response.status_code == 200
        assert b"No component" in response.content


@pytest.mark.django_db
class TestSecurityReportSandbox:
    def test_sandbox_disabled(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory()
        # Sandbox is disabled by default (no config).
        response = client.post(f"/security/{report.pk}/sandbox/")
        assert response.status_code == 200
        assert b"not enabled" in response.content

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_sandbox_enqueues_worker_command(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        # The web container does not have Docker access. The view should
        # enqueue a WorkerCommand for the worker to execute the sandbox,
        # not run run_poc_in_sandbox inline.
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, sandbox_enabled=True),
        )
        report = SecurityReportFactory(parsed_poc="echo test")

        with patch("franktheunicorn.security.sandbox.run_poc_in_sandbox") as mock_sandbox:
            response = client.post(f"/security/{report.pk}/sandbox/")

        assert response.status_code == 200
        assert b"queued" in response.content.lower()
        # Sandbox must NOT have run inline.
        mock_sandbox.assert_not_called()
        # And the worker command must exist.
        assert WorkerCommand.objects.filter(
            command="run_security_sandbox", security_report=report, status="pending"
        ).exists()


@pytest.mark.django_db
class TestSecurityReportFeedback:
    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_agree_records_feedback_and_returns_fragment(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import OperatorConfig

        mock_config.return_value = OperatorConfig(github_username="testuser")
        report = SecurityReportFactory(triage_summary="Looks legit.", assessed_severity="high")

        response = client.post(
            f"/security/{report.pk}/feedback/",
            {"agreed": "yes", "comment": "Solid analysis"},
        )
        assert response.status_code == 200
        assert b"Agreed" in response.content

        feedback = SecurityTriageFeedback.objects.get()
        assert feedback.report == report
        assert feedback.agreed is True
        assert feedback.operator_comment == "Solid analysis"
        assert feedback.triage_summary_snapshot == "Looks legit."
        assert feedback.assessed_severity_snapshot == "high"

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_disagree_records_feedback(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import OperatorConfig

        mock_config.return_value = OperatorConfig(github_username="testuser")
        report = SecurityReportFactory()

        response = client.post(
            f"/security/{report.pk}/feedback/",
            {"agreed": "no", "comment": "This is expected behavior."},
        )
        assert response.status_code == 200
        assert b"Disagreed" in response.content

        feedback = SecurityTriageFeedback.objects.get()
        assert feedback.agreed is False

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_shows_current_learned_guidance(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import OperatorConfig

        mock_config.return_value = OperatorConfig(github_username="testuser")
        project = ProjectFactory()
        SecurityTriageGuidanceFactory(project=project, guidance_text="Watch for X pattern.")
        report = SecurityReportFactory(project=project)

        response = client.post(
            f"/security/{report.pk}/feedback/",
            {"agreed": "yes", "comment": ""},
        )
        assert response.status_code == 200
        assert b"Watch for X pattern." in response.content

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_no_llm_backend_still_records_feedback(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """No LLM backend configured means distillation is skipped, but the
        feedback row itself must still save (degrade gracefully, not fail)."""
        from franktheunicorn.config.models import OperatorConfig

        mock_config.return_value = OperatorConfig(github_username="testuser")
        report = SecurityReportFactory()

        response = client.post(f"/security/{report.pk}/feedback/", {"agreed": "yes"})
        assert response.status_code == 200
        assert SecurityTriageFeedback.objects.filter(report=report).exists()

    def test_feedback_404_for_missing_report(self, client: Client, db: Any) -> None:
        response = client.post("/security/99999/feedback/", {"agreed": "yes"})
        assert response.status_code == 404


@pytest.mark.django_db
class TestSecurityGuidanceList:
    def test_renders_empty_state(self, client: Client) -> None:
        response = client.get("/security/guidance/")
        assert response.status_code == 200
        assert b"No learned guidance yet" in response.content

    def test_renders_active_guidance_rows(self, client: Client, db: Any) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityTriageGuidanceFactory(
            project=project,
            guidance_text="- Treat model-loading RCE as expected.",
            source_feedback_count=3,
        )
        SecurityTriageGuidanceFactory(project=None, guidance_text="- Global rule.")

        response = client.get("/security/guidance/")
        body = response.content.decode()
        assert response.status_code == 200
        assert "Treat model-loading RCE as expected." in body
        assert "Global rule." in body
        assert "3" in body

    def test_inactive_guidance_excluded(self, client: Client, db: Any) -> None:
        SecurityTriageGuidanceFactory(guidance_text="stale guidance", is_active=False)

        response = client.get("/security/guidance/")
        assert b"stale guidance" not in response.content


@pytest.mark.django_db
class TestTriageFailureVisibleAlongsideSummary:
    def test_failed_retriage_shows_even_when_a_summary_exists(
        self, client: Client, db: Any
    ) -> None:
        """A stale summary must not make a failed re-run look like success."""
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(triage_summary="An earlier verdict.")
        cmd = WorkerCommand.objects.create(command="run_security_triage", security_report=report)
        WorkerCommand.objects.filter(pk=cmd.pk).update(
            status="failed", error="RuntimeError: model timed out"
        )

        body = client.get(f"/security/{report.pk}/").content

        assert b"Triage failed in the worker" in body
        assert b"model timed out" in body
        assert b"An earlier verdict." in body  # the old result is still shown
        assert b"Re-run LLM Triage" in body
