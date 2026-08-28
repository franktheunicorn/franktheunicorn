"""Tests for security report dashboard views."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
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
class TestSecurityReportListRanking:
    """A 129-finding archive is unusable in arrival order, and the page shows 100."""

    def test_highest_priority_first_by_default(self, client: Client) -> None:
        SecurityReportFactory(title="Low ranked", priority=20.0)
        SecurityReportFactory(title="High ranked", priority=90.0)

        content = client.get("/security/").content

        assert content.index(b"High ranked") < content.index(b"Low ranked")

    def test_newest_is_still_available(self, client: Client) -> None:
        """A trickle of emailed reports all rank 0.0, so arrival order still matters."""
        SecurityReportFactory(title="High ranked", priority=90.0)
        SecurityReportFactory(title="Low ranked", priority=20.0)

        content = client.get("/security/?sort=newest").content

        assert content.index(b"Low ranked") < content.index(b"High ranked")

    def test_an_unknown_sort_falls_back_rather_than_erroring(self, client: Client) -> None:
        SecurityReportFactory(title="Only one", priority=1.0)

        response = client.get("/security/?sort=; DROP TABLE")

        assert response.status_code == 200
        assert b"Only one" in response.content

    def test_the_rank_is_accounted_for_in_the_row(self, client: Client) -> None:
        """A number nobody can account for is a number nobody trusts."""
        SecurityReportFactory(
            title="Ranked", priority=112.3, priority_reason="HIGH, true_positive, CVSS 7.5"
        )

        content = client.get("/security/").content.decode()

        assert "HIGH, true_positive, CVSS 7.5" in content

    def test_the_status_tabs_keep_the_sort(self, client: Client) -> None:
        SecurityReportFactory(title="Anything", status="valid")

        content = client.get("/security/?sort=newest").content.decode()

        assert "?sort=newest&amp;status=valid" in content


@pytest.mark.django_db
class TestSecurityArchiveDrop:
    """The undo for a bad import."""

    def test_the_archive_is_listed_with_its_counts(self, client: Client) -> None:
        SecurityReportFactory(source="zip", source_archive="scan-spark.zip", finding_id="f001")
        SecurityReportFactory(source="zip", source_archive="scan-spark.zip", finding_id="")

        content = client.get("/security/").content.decode()

        assert "scan-spark.zip" in content
        assert "Drop" in content

    def test_a_report_with_no_archive_is_not_listed(self, client: Client) -> None:
        SecurityReportFactory(title="Pasted one", source="paste", source_archive="")

        content = client.get("/security/").content.decode()

        assert "Imported archives" not in content

    def test_dropping_deletes_every_report_from_that_archive(self, client: Client) -> None:
        SecurityReportFactory(source_archive="bad.zip")
        SecurityReportFactory(source_archive="bad.zip")
        keeper = SecurityReportFactory(source_archive="good.zip")

        response = client.post("/security/drop-archive/", {"archive": "bad.zip"}, follow=True)

        assert response.status_code == 200
        assert list(SecurityReport.objects.values_list("pk", flat=True)) == [keeper.pk]

    def test_a_queued_triage_goes_with_the_report(self, client: Client) -> None:
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(source_archive="bad.zip")
        WorkerCommand.objects.create(command="run_security_triage", security_report=report)

        client.post("/security/drop-archive/", {"archive": "bad.zip"})

        assert WorkerCommand.objects.count() == 0

    def test_operator_feedback_outlives_the_report(self, client: Client) -> None:
        """Those rows are the operator's judgement, and the learning loop distils
        them. Re-importing an archive shouldn't have to re-earn them."""
        report = SecurityReportFactory(source_archive="bad.zip")
        SecurityTriageFeedback.objects.create(report=report, agreed=False)

        client.post("/security/drop-archive/", {"archive": "bad.zip"})

        assert SecurityTriageFeedback.objects.count() == 1
        assert SecurityTriageFeedback.objects.get().report is None

    def test_the_count_is_reported_including_the_triaged_ones(self, client: Client) -> None:
        SecurityReportFactory(source_archive="bad.zip", status="new")
        SecurityReportFactory(source_archive="bad.zip", status="valid")

        response = client.post("/security/drop-archive/", {"archive": "bad.zip"}, follow=True)

        body = response.content.decode()
        assert "Dropped 2 report(s) from bad.zip" in body
        assert "1 had already been triaged" in body

    def test_a_partial_label_matches_nothing(self, client: Client) -> None:
        """Fuzzy matching here deletes reports the operator didn't name."""
        SecurityReportFactory(source_archive="scan-spark-20260811.zip")

        client.post("/security/drop-archive/", {"archive": "scan-spark"})

        assert SecurityReport.objects.count() == 1

    def test_an_empty_label_drops_nothing(self, client: Client) -> None:
        """Otherwise it would match every pasted and emailed report at once."""
        SecurityReportFactory(source_archive="")
        SecurityReportFactory(source_archive="scan.zip")

        response = client.post("/security/drop-archive/", {"archive": "  "}, follow=True)

        assert SecurityReport.objects.count() == 2
        assert "No archive named" in response.content.decode()

    def test_an_already_dropped_archive_says_so(self, client: Client) -> None:
        """Rather than reporting a successful drop of zero reports, which reads as
        a broken button."""
        response = client.post("/security/drop-archive/", {"archive": "gone.zip"}, follow=True)

        assert "No reports left from gone.zip" in response.content.decode()

    def test_get_is_refused(self, client: Client) -> None:
        SecurityReportFactory(source_archive="bad.zip")

        response = client.get("/security/drop-archive/?archive=bad.zip")

        assert response.status_code == 405
        assert SecurityReport.objects.count() == 1

    def test_htmx_gets_a_full_navigation_not_a_fragment(self, client: Client) -> None:
        """The drop changes its own row, the list and every tab count, and a
        swapped fragment never renders the flash message."""
        SecurityReportFactory(source_archive="bad.zip")

        response = client.post(
            "/security/drop-archive/", {"archive": "bad.zip"}, headers={"hx-request": "true"}
        )

        assert response.status_code == 204
        assert response["HX-Redirect"] == "/security/"
        assert SecurityReport.objects.count() == 0


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

    def test_a_severity_only_run_says_what_it_got_and_what_it_didnt(
        self, client: Client, db: Any
    ) -> None:
        """_parse_report writes the severity two LLM calls before any verdict.

        So this is the commonest partial failure, and both halves have to show:
        blanket "produced no assessment" contradicts the badge on the page, while
        counting severity as a result rendered the result partial — which gates
        every block on the other fields — as an empty heading.
        """
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(
            triage_summary="",
            poc_assessment="",
            expected_behavior_explanation="",
            is_expected_behavior=False,
            poc_plausible=None,
            assessed_severity="high",
        )
        cmd = WorkerCommand.objects.create(command="run_security_triage", security_report=report)
        WorkerCommand.objects.filter(pk=cmd.pk).update(status="completed")

        body = client.get(f"/security/{report.pk}/").content.decode()

        # Contiguous phrases only — the template wraps mid-sentence.
        assert "came back empty" in body
        assert "rated this" in body
        assert "High" in body
        # The empty-card path: the result partial must not be rendered at all.
        assert "Triage Analysis" not in body
        # And the re-run button is still there.
        assert "Re-run LLM Triage" in body

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
    def test_the_cve_button_works_without_the_feature_flag_too(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """Same argument as the triage button, and this one was still gated.

        One NVD lookup for the report you're looking at. The gate left "Check CVE
        Database" a permanent no-op on a default install, pointing at a key the
        operator's file doesn't contain.
        """
        from franktheunicorn.config.models import OperatorConfig

        mock_config.return_value = OperatorConfig(github_username="testuser")
        report = SecurityReportFactory(parsed_component="libarchive")

        with patch(
            "franktheunicorn.security.cve_lookup.search_cves", return_value=[]
        ) as mock_search:
            response = client.post(f"/security/{report.pk}/cve-check/")

        assert b"not enabled" not in response.content
        assert mock_search.called

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


@pytest.mark.django_db
class TestSecurityCsvRoundTrip:
    """The dashboard door onto the shared-spreadsheet round trip.

    The module tests (tests/security/test_sheet_sync.py) cover the semantics; these
    cover the bit that only exists at the HTTP boundary — the streaming response,
    the BOM a real Sheets download carries, and the multi-line cell that a naive
    upload handler mangles.
    """

    def _csv(self, client: Client, query: str = "") -> str:
        response = client.get(f"/security/export.csv{query}")
        assert response.status_code == 200
        return b"".join(response.streaming_content).decode()

    def test_export_streams_a_csv_attachment(self, client: Client) -> None:
        SecurityReportFactory(title="Path traversal in the loader")
        response = client.get("/security/export.csv")

        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")
        assert "attachment; filename=" in response["Content-Disposition"]
        assert ".csv" in response["Content-Disposition"]
        body = b"".join(response.streaming_content).decode()
        assert "report_id,check," in body
        assert "Path traversal in the loader" in body

    def test_export_honours_the_status_tab(self, client: Client) -> None:
        SecurityReportFactory(title="still new", status="new")
        SecurityReportFactory(title="already ruled", status="valid")

        body = self._csv(client, "?status=valid")

        assert "already ruled" in body
        assert "still new" not in body

    def test_export_refuses_a_bogus_status_rather_than_widening(self, client: Client) -> None:
        """Exporting everything for an unknown filter hands back a wider sheet than
        the operator asked for, which is the wrong way to be wrong about this."""
        assert client.get("/security/export.csv?status=nonsense").status_code == 400

    def test_full_export_carries_the_payload_and_the_plain_one_does_not(
        self, client: Client
    ) -> None:
        SecurityReportFactory(raw_text="the proof of concept text")

        assert "the proof of concept text" not in self._csv(client)
        assert "the proof of concept text" in self._csv(client, "?full=1")

    def test_the_export_cap_is_real_and_takes_the_top_of_the_ranking(self, client: Client) -> None:
        """A cap nobody tested is a cap that silently isn't applied — and if it
        cut from the wrong end it would drop exactly the reports worth reviewing."""
        SecurityReportFactory(title="ranked first", priority=90.0)
        SecurityReportFactory(title="ranked second", priority=50.0)
        SecurityReportFactory(title="ranked last", priority=1.0)

        with patch("franktheunicorn.dashboard.views.MAX_SECURITY_CSV_EXPORT_ROWS", 2):
            body = self._csv(client)

        assert "ranked first" in body
        assert "ranked second" in body
        assert "ranked last" not in body

    def test_import_applies_an_edited_verdict(self, client: Client) -> None:
        report = SecurityReportFactory(status="new")
        edited = self._csv(client).replace(",new,", ",valid,")
        upload = SimpleUploadedFile("reviewed.csv", edited.encode(), content_type="text/csv")

        response = client.post("/security/import-csv/", {"csv_file": upload}, follow=True)

        assert response.status_code == 200
        report.refresh_from_db()
        assert report.status == "valid"

    def test_import_survives_the_bom_a_real_sheets_download_carries(self, client: Client) -> None:
        """Sheets and Excel both write a UTF-8 BOM. Without stripping it the first
        header reads as "﻿report_id" and the whole file looks like somebody
        else's CSV."""
        report = SecurityReportFactory(status="new")
        edited = self._csv(client).replace(",new,", ",valid,")
        upload = SimpleUploadedFile(
            "reviewed.csv", b"\xef\xbb\xbf" + edited.encode(), content_type="text/csv"
        )

        client.post("/security/import-csv/", {"csv_file": upload}, follow=True)

        report.refresh_from_db()
        assert report.status == "valid"

    def test_import_keeps_the_line_breaks_in_a_multiline_comment(self, client: Client) -> None:
        """str.splitlines() deletes the newline inside a quoted cell and runs the
        words either side of it together. This is that regression, at the door it
        actually happened at."""
        report = SecurityReportFactory()
        # A reviewer's two-line comment, quoted the way a spreadsheet writes it.
        rows = self._csv(client).split("\n")
        header = rows[0].split(",")
        note_index = header.index("external_notes")
        cells = next(row for row in rows[1:] if row.startswith(f"{report.pk},")).split(",")
        cells[note_index] = '"first line\nsecond line"'
        edited = "\n".join([rows[0], ",".join(cells)]) + "\n"
        upload = SimpleUploadedFile("reviewed.csv", edited.encode(), content_type="text/csv")

        client.post("/security/import-csv/", {"csv_file": upload}, follow=True)

        report.refresh_from_db()
        assert report.external_notes == "first line\nsecond line"

    def test_import_reports_a_conflict_instead_of_reverting_a_ruling(self, client: Client) -> None:
        report = SecurityReportFactory(status="new")
        edited = self._csv(client).replace(",new,", ",invalid,")
        # The operator rules on it while the sheet is out with the PMC.
        report.status = "valid"
        report.save()
        upload = SimpleUploadedFile("reviewed.csv", edited.encode(), content_type="text/csv")

        response = client.post("/security/import-csv/", {"csv_file": upload}, follow=True)

        report.refresh_from_db()
        assert report.status == "valid"
        body = response.content.decode()
        assert "conflicted" in body
        assert f"report {report.pk}" in body

    def test_force_checkbox_lets_the_sheet_win(self, client: Client) -> None:
        report = SecurityReportFactory(status="new")
        edited = self._csv(client).replace(",new,", ",invalid,")
        report.status = "valid"
        report.save()
        upload = SimpleUploadedFile("reviewed.csv", edited.encode(), content_type="text/csv")

        client.post("/security/import-csv/", {"csv_file": upload, "force": "on"}, follow=True)

        report.refresh_from_db()
        assert report.status == "invalid"

    def test_dry_run_checkbox_writes_nothing(self, client: Client) -> None:
        report = SecurityReportFactory(status="new")
        edited = self._csv(client).replace(",new,", ",valid,")
        upload = SimpleUploadedFile("reviewed.csv", edited.encode(), content_type="text/csv")

        response = client.post(
            "/security/import-csv/", {"csv_file": upload, "dry_run": "on"}, follow=True
        )

        report.refresh_from_db()
        assert report.status == "new"
        assert "would apply" in response.content.decode()

    def test_import_with_no_file_says_so(self, client: Client) -> None:
        response = client.post("/security/import-csv/", {}, follow=True)
        assert "Choose a reviewed" in response.content.decode()

    def test_import_rejects_a_spreadsheet_binary(self, client: Client) -> None:
        upload = SimpleUploadedFile(
            "reviewed.xlsx",
            b"PK\x03\x04\xff\xfe\x00binary",
            content_type="application/vnd.ms-excel",
        )
        response = client.post("/security/import-csv/", {"csv_file": upload}, follow=True)
        assert "Comma-separated values" in response.content.decode()

    def test_an_impossible_report_id_does_not_500_the_upload(self, client: Client) -> None:
        """A twenty-digit id parses as a Python int and then raises OverflowError out
        of the pk__in query. Unhandled, that is a 500 on an endpoint anybody on the
        Tailscale net can POST to."""
        SecurityReportFactory(status="new")
        rows = self._csv(client).splitlines()
        cells = rows[1].split(",")
        cells[0] = "9" * 20
        payload = f"{rows[0]}\n{','.join(cells)}\n"
        upload = SimpleUploadedFile("reviewed.csv", payload.encode(), content_type="text/csv")

        response = client.post("/security/import-csv/", {"csv_file": upload}, follow=True)

        assert response.status_code == 200
        assert "no-id" in response.content.decode()

    def test_import_rejects_someone_elses_csv(self, client: Client) -> None:
        upload = SimpleUploadedFile(
            "scan.csv", b"finding,severity\nsome scan,high\n", content_type="text/csv"
        )
        response = client.post("/security/import-csv/", {"csv_file": upload}, follow=True)
        assert "report_id" in response.content.decode()

    def test_import_is_post_only(self, client: Client) -> None:
        assert client.get("/security/import-csv/").status_code == 405

    def test_the_list_page_offers_the_round_trip(self, client: Client) -> None:
        body = client.get("/security/").content.decode()
        assert "Export CSV" in body
        assert "Import edits" in body
        # The guard column is the whole safety story; the page has to say so.
        assert "check" in body

    def test_a_no_op_verdict_save_does_not_delete_the_pmc_s_cve(self, client: Client) -> None:
        """The verdict form used to blank matched_cve_id for any status but
        "duplicate". Harmless while that form was the only writer; silent data loss
        once the review sheet could set it too — the operator opens the report,
        saves without touching anything, and the PMC's reference is gone."""
        report = SecurityReportFactory(status="valid", matched_cve_id="CVE-2026-1234")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "operator_notes": "", "matched_cve_id": "CVE-2026-1234"},
        )

        report.refresh_from_db()
        assert report.matched_cve_id == "CVE-2026-1234"

    def test_clearing_the_cve_field_still_clears_it(self, client: Client) -> None:
        report = SecurityReportFactory(status="valid", matched_cve_id="CVE-2026-1234")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "operator_notes": "", "matched_cve_id": ""},
        )

        report.refresh_from_db()
        assert report.matched_cve_id == ""

    def test_flipping_away_from_duplicate_still_drops_the_reference(self, client: Client) -> None:
        """The one case where dropping it is right, and why the old blanket rule
        existed: "actually valid" means the CVE it duplicated no longer describes
        it. Kept working — see test_clearing_duplicate_clears_cve_id."""
        report = SecurityReportFactory(status="duplicate", matched_cve_id="CVE-2026-1234")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "operator_notes": "", "matched_cve_id": "CVE-2026-1234"},
        )

        report.refresh_from_db()
        assert report.matched_cve_id == ""

    def test_a_forced_overwrite_names_the_report_it_overwrote(self, client: Client) -> None:
        """Filtering the per-row messages on outcome alone skipped every applied row,
        so a forced import flashed a bare "applied 1" and the operator's own ruling
        was gone with nothing saying which report."""
        report = SecurityReportFactory(status="new")
        edited = self._csv(client).replace(",new,", ",invalid,")
        report.status = "valid"
        report.save()
        upload = SimpleUploadedFile("reviewed.csv", edited.encode(), content_type="text/csv")

        response = client.post(
            "/security/import-csv/", {"csv_file": upload, "force": "on"}, follow=True
        )

        body = response.content.decode()
        assert "forced over newer work" in body
        assert f"report {report.pk}" in body
        assert "forced over a newer state" in body

    def test_a_valid_report_with_a_cve_is_not_labelled_a_duplicate(self, client: Client) -> None:
        """A CVE here no longer implies duplicate — the verdict form keeps the field
        for every status and the sheet writes it independently — so the exact PMC
        ruling this feature carries was displayed as "this is a duplicate"."""
        SecurityReportFactory(title="Real one", status="valid", matched_cve_id="CVE-2026-1234")

        body = client.get("/security/").content.decode()

        assert "CVE-2026-1234" in body
        assert "Dup of" not in body
        assert "Related" in body

    def test_a_genuine_duplicate_still_says_dup_of(self, client: Client) -> None:
        SecurityReportFactory(status="duplicate", matched_cve_id="CVE-2026-1234")
        assert "Dup of" in client.get("/security/").content.decode()

    def test_the_verdict_form_rejects_an_overlong_cve(self, client: Client) -> None:
        """SQLite doesn't enforce max_length, so a 309-character "CVE" persisted here
        and would be a DataError — an unhandled 500 losing the status and notes in the
        same save() — on the Postgres install DATABASE_URL promises."""
        report = SecurityReportFactory(status="valid")

        response = client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "matched_cve_id": "CVE-2026-" + "9" * 300},
        )

        assert response.status_code == 400
        report.refresh_from_db()
        assert report.matched_cve_id == ""

    def test_the_export_says_it_is_capped_before_you_click(self, client: Client) -> None:
        """A cap applied silently means the operator shares "the backlog" and the
        bottom of it isn't in the file."""
        for _ in range(3):
            SecurityReportFactory()

        with patch("franktheunicorn.dashboard.views.MAX_SECURITY_CSV_EXPORT_ROWS", 2):
            body = client.get("/security/").content.decode()

        assert "stops at" in body
        assert "export_security_csv" in body

    def test_no_cap_notice_when_everything_fits(self, client: Client) -> None:
        SecurityReportFactory()
        assert "stops at" not in client.get("/security/").content.decode()

    def test_the_export_honours_the_sort_the_page_is_showing(self, client: Client) -> None:
        SecurityReportFactory(title="ranked high", priority=90.0)
        SecurityReportFactory(title="arrived later", priority=1.0)

        body = self._csv(client, "?sort=newest")

        assert body.index("arrived later") < body.index("ranked high")
        # And the list page hands the sort to the export link, or the setting is moot.
        assert "sort=newest" in client.get("/security/?sort=newest").content.decode()

    def test_the_csv_is_not_cacheable(self, client: Client) -> None:
        """Unfixed vulnerability reports — with full=1, a working description of how
        to exploit them — persisting in the disk cache of whatever laptop reached the
        dashboard over Tailscale."""
        SecurityReportFactory()
        response = client.get("/security/export.csv?full=1")
        assert response["Cache-Control"] == "no-store"

    def test_the_download_filename_says_which_slice(self, client: Client) -> None:
        SecurityReportFactory(status="valid")
        response = client.get("/security/export.csv?status=valid")
        assert "-valid-" in response["Content-Disposition"]

    def test_a_pmc_comment_is_visible_on_the_report(self, client: Client) -> None:
        """An import that lands a ruling in a field no page shows is worse than no
        import: the operator then rules on the report believing nobody else has."""
        report = SecurityReportFactory(external_notes="PMC: this is a real one, CVE it")

        body = client.get(f"/security/{report.pk}/").content.decode()

        assert "From the review sheet" in body
        assert "PMC: this is a real one, CVE it" in body
