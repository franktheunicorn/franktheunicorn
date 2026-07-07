"""Tests for security report dashboard views."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client

from franktheunicorn.core.models import SecurityReport
from tests.factories import (
    EmailScanRecordFactory,
    ProjectFactory,
    SecurityReportFactory,
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
    @patch("franktheunicorn.security.triage.triage_report")
    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_triage_endpoint(
        self, mock_config: MagicMock, mock_triage: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory(title="Test triage")

        response = client.post(f"/security/{report.pk}/triage/")
        assert response.status_code == 200
        mock_triage.assert_called_once()

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

    @patch("franktheunicorn.security.triage.triage_report", side_effect=RuntimeError("boom"))
    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_triage_error_returns_error_html(
        self, mock_config: MagicMock, mock_triage: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory()

        response = client.post(f"/security/{report.pk}/triage/")
        assert response.status_code == 200
        assert b"Triage failed" in response.content


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
