"""Tests for security report dashboard views."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from franktheunicorn.core.models import SecurityReport, SecurityTriageFeedback
from tests.factories import (
    CannedLLMBackend,
    EmailScanRecordFactory,
    ProjectFactory,
    SecurityRecheckRunFactory,
    SecurityReportFactory,
    SecurityTriageGuidanceFactory,
    cursor_response,
    make_operator_config,
    patched_report,
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
class TestSecurityCveWithoutBranchTab:
    """A CVE id is assigned and nothing is recorded as fixing it — the queue an
    operator holding a CVE number works from."""

    def test_the_tab_lists_a_cve_with_no_branch(self, client: Client) -> None:
        SecurityReportFactory(
            title="Needs a branch", status="valid", matched_cve_id="CVE-2026-1111"
        )

        content = client.get("/security/?status=cve-no-branch").content.decode()

        assert "Needs a branch" in content

    def test_a_report_with_a_branch_is_not_listed(self, client: Client) -> None:
        SecurityReportFactory(
            title="Already fixed",
            status="valid",
            matched_cve_id="CVE-2026-2222",
            fixed_in_branch="branch-3.5",
        )

        content = client.get("/security/?status=cve-no-branch").content.decode()

        assert "Already fixed" not in content

    def test_a_report_with_no_cve_is_not_listed(self, client: Client) -> None:
        SecurityReportFactory(title="No CVE yet", status="valid", matched_cve_id="")

        content = client.get("/security/?status=cve-no-branch").content.decode()

        assert "No CVE yet" not in content

    @pytest.mark.parametrize("status", ["invalid", "expected-behavior", "duplicate"])
    def test_a_status_owing_no_fix_is_not_listed(self, client: Client, status: str) -> None:
        """Without this the queue on a real backlog is almost entirely "dup of
        CVE-X", which is what matched_cve_id was built for."""
        SecurityReportFactory(title="Owes nothing", status=status, matched_cve_id="CVE-2026-3333")

        content = client.get("/security/?status=cve-no-branch").content.decode()

        assert "Owes nothing" not in content

    def test_the_tab_count_matches_what_the_tab_lists(self, client: Client) -> None:
        """Count and contents were two separate expressions; they read one Q now."""
        SecurityReportFactory(title="Listed", status="valid", matched_cve_id="CVE-2026-4444")
        SecurityReportFactory(
            title="Has branch",
            status="valid",
            matched_cve_id="CVE-2026-5555",
            fixed_in_branch="master",
        )
        SecurityReportFactory(title="No CVE", status="valid")

        response = client.get("/security/?status=cve-no-branch")

        tabs = {tab["key"]: tab["count"] for tab in response.context["status_tabs"]}
        assert tabs["cve-no-branch"] == 1
        assert [report.title for report in response.context["reports"]] == ["Listed"]

    def test_the_branch_is_shown_on_the_row(self, client: Client) -> None:
        """A count nobody can account for row by row is a count nobody believes."""
        SecurityReportFactory(title="Fixed one", status="valid", fixed_in_branch="branch-4.0")

        content = client.get("/security/").content.decode()

        assert "fixed in branch-4.0" in content

    def test_a_machine_linked_duplicate_is_not_listed(self, client: Client) -> None:
        """security.duplicates links without judging and never sets the status, so
        six findings of one missing check would otherwise be six rows."""
        keeper = SecurityReportFactory(status="valid", matched_cve_id="CVE-2026-8888")
        SecurityReportFactory(
            title="Linked twin",
            status="valid",
            matched_cve_id="CVE-2026-8888",
            duplicate_of=keeper,
        )

        content = client.get("/security/?status=cve-no-branch").content.decode()

        assert "Linked twin" not in content

    def test_the_row_cap_is_said_on_the_page(self, client: Client) -> None:
        """The badge counts the whole set; the page shows 100. On a queue meant to
        reach zero, a count the rows can't account for is the wrong kind of wrong."""
        for index in range(102):
            SecurityReportFactory(status="valid", matched_cve_id=f"CVE-2026-{9000 + index}")

        content = client.get("/security/?status=cve-no-branch").content.decode()

        assert "Showing the top 100 of 102" in content
        # And it points somewhere real: there is no page 2, and the CSV button caps too.
        assert "no page 2" in content

    def test_the_cap_notice_is_absent_when_everything_fits(self, client: Client) -> None:
        SecurityReportFactory(status="valid", matched_cve_id="CVE-2026-1212")

        content = client.get("/security/?status=cve-no-branch").content.decode()

        assert "Showing the top" not in content

    def test_a_crafted_status_cannot_smuggle_params_into_the_export_href(
        self, client: Client
    ) -> None:
        """?status=new%26full=1 rendered status=new&full=1 in the plain Export href
        (autoescaped to &amp;, which the browser decodes back), so the ordinary
        button handed back the --full export: raw report text and proposed patches."""
        SecurityReportFactory(title="A report", status="new", raw_text="POC-BODY-HERE")

        import re

        content = client.get("/security/?status=new%26full=1").content.decode()

        # The plain Export button: the one that must never carry full=1.
        plain = re.findall(r'href="([^"]*export\.csv\?sort=[^"]*)"', content)
        assert plain, "expected the plain export href to render"
        assert all("full=1" not in href for href in plain), plain

    def test_an_empty_status_does_not_show_a_clear_tab_over_a_full_export(
        self, client: Client
    ) -> None:
        """?status= filtered to Q(status="") — nothing — while the export read the
        same empty value as "no filter": one click from "this slice is clear" to
        mailing out the whole backlog."""
        SecurityReportFactory(title="Still here", status="new")

        response = client.get("/security/?status=")

        assert response.context["active_status"] == "all"
        assert [r.title for r in response.context["reports"]] == ["Still here"]

    def test_an_unknown_status_falls_back_rather_than_rendering_a_dead_button(
        self, client: Client
    ) -> None:
        """It used to render 200 with an Export button that 400s."""
        SecurityReportFactory(title="Still here", status="new")

        response = client.get("/security/?status=garbage")

        assert response.status_code == 200
        assert response.context["active_status"] == "all"

    def test_an_empty_tab_does_not_claim_the_backlog_is_empty(self, client: Client) -> None:
        SecurityReportFactory(title="Untriaged", status="new")

        content = client.get("/security/?status=cve-no-branch").content.decode()

        assert "No reports on this tab" in content
        assert "No security reports yet" not in content

    def test_the_export_button_on_this_tab_works(self, client: Client) -> None:
        """The export honours the page's filter, and this filter is not a status —
        so the button on this tab handed back a 400 until the export learned it."""
        SecurityReportFactory(
            title="Needs a branch", status="valid", matched_cve_id="CVE-2026-7777"
        )
        SecurityReportFactory(title="Has one", status="valid", fixed_in_branch="master")

        response = client.get("/security/export.csv?status=cve-no-branch")

        assert response.status_code == 200
        body = b"".join(response.streaming_content).decode()
        assert "Needs a branch" in body
        assert "Has one" not in body

    def test_the_tab_keeps_the_sort(self, client: Client) -> None:
        SecurityReportFactory(status="valid", matched_cve_id="CVE-2026-6666")

        content = client.get("/security/?sort=newest").content.decode()

        assert "?sort=newest&amp;status=cve-no-branch" in content


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

    def test_dropping_cancels_pending_jobs_and_leaves_other_archives_alone(
        self, client: Client
    ) -> None:
        from franktheunicorn.core.models import WorkerCommand

        doomed = SecurityReportFactory(source_archive="bad.zip")
        keeper = SecurityReportFactory(source_archive="good.zip")
        WorkerCommand.objects.create(command="run_security_triage", security_report=doomed)
        WorkerCommand.objects.create(command="map_report_versions", security_report=doomed)
        WorkerCommand.objects.create(command="verify_security_report", security_report=doomed)
        keep_cmd = WorkerCommand.objects.create(
            command="run_security_triage", security_report=keeper
        )

        response = client.post("/security/drop-archive/", {"archive": "bad.zip"}, follow=True)

        assert SecurityReport.objects.filter(pk=keeper.pk).exists()
        remaining = list(WorkerCommand.objects.values_list("pk", "command", "status"))
        assert remaining == [(keep_cmd.pk, "run_security_triage", "pending")]
        assert "cancelled 3 queued job(s)" in response.content.decode()

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

    def test_the_fix_branch_is_recorded(self, client: Client, db: Any) -> None:
        """Which branch carries the fix is the operator's answer, typed here."""
        report = SecurityReportFactory(status="new")

        response = client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "fixed_in_branch": "  branch-3.5  "},
        )

        assert response.status_code == 200
        report.refresh_from_db()
        assert report.fixed_in_branch == "branch-3.5"

    def test_a_post_without_the_branch_field_does_not_blank_it(
        self, client: Client, db: Any
    ) -> None:
        """The CVE beside it lost data exactly this way — see the view's comment."""
        report = SecurityReportFactory(status="valid", fixed_in_branch="branch-4.0")

        client.post(f"/security/{report.pk}/verdict/", {"status": "valid"})

        report.refresh_from_db()
        assert report.fixed_in_branch == "branch-4.0"

    def test_submitting_an_empty_branch_clears_it(self, client: Client, db: Any) -> None:
        """A branch that turned out not to fix it has to be removable."""
        report = SecurityReportFactory(status="valid", fixed_in_branch="wrong-branch")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "fixed_in_branch": ""},
        )

        report.refresh_from_db()
        assert report.fixed_in_branch == ""

    def test_a_long_multi_branch_answer_is_kept(self, client: Client, db: Any) -> None:
        """The realistic answer names several branches with commentary, which ran past
        the 200-char column this field started as."""
        report = SecurityReportFactory(status="new")
        answer = "master, branch-4.0, branch-3.5 (backport pending), branch-3.4 " + "x" * 200

        response = client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "fixed_in_branch": answer},
        )

        assert response.status_code == 200
        report.refresh_from_db()
        assert report.fixed_in_branch == answer

    def test_invisible_characters_are_cleaned_out_of_the_branch(
        self, client: Client, db: Any
    ) -> None:
        """A NUL survives .strip() and Postgres refuses it in a string; a zero-width
        space is not even whitespace, so it stored a value that looks empty, renders
        as nothing, and can never match fixed_in_branch="" again — a report silently
        out of the queue. Cleaned rather than refused: the operator cannot see any of
        these, so a 400 tells them nothing (and htmx would not show it)."""
        report = SecurityReportFactory(status="new")

        response = client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "fixed_in_branch": "mas\u200bter\x00-3.5"},
        )

        assert response.status_code == 200
        report.refresh_from_db()
        assert report.fixed_in_branch == "master-3.5"

    def test_a_zero_width_only_branch_is_stored_as_empty(self, client: Client, db: Any) -> None:
        """Otherwise the row leaves the queue while rendering as "fixed in" nothing."""
        report = SecurityReportFactory(status="new")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "fixed_in_branch": "\u200b\ufeff"},
        )

        report.refresh_from_db()
        assert report.fixed_in_branch == ""
        assert report.operator_has_ruled is False

    def test_a_two_line_paste_does_not_merge_on_the_next_save(
        self, client: Client, db: Any
    ) -> None:
        """A stored newline is stripped by the <input type="text"> on re-render, so
        the next Save persisted "branch-3.5branch-4.0" — a branch that never existed."""
        report = SecurityReportFactory(status="new")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "fixed_in_branch": "branch-3.5\nbranch-4.0"},
        )

        report.refresh_from_db()
        assert report.fixed_in_branch == "branch-3.5 branch-4.0"

    def test_a_partial_post_does_not_blank_the_notes(self, client: Client, db: Any) -> None:
        """operator_notes is writable from the review sheet too, so a POST that
        never showed the operator the textarea must not delete a PMC's comment."""
        report = SecurityReportFactory(status="valid", operator_notes="The PMC said no.")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "fixed_in_branch": "master"},
        )

        report.refresh_from_db()
        assert report.operator_notes == "The PMC said no."
        assert report.fixed_in_branch == "master"

    def test_the_branch_survives_the_learning_feedback_path(self, client: Client, db: Any) -> None:
        """The save has two update_fields lists and only one gets exercised by a
        report with a staged suggestion."""
        report = SecurityReportFactory(status="new", auto_triage_status="valid")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "fixed_in_branch": "branch-3.5"},
        )

        report.refresh_from_db()
        assert report.fixed_in_branch == "branch-3.5"

    def test_a_verdict_against_the_staged_suggestion_records_disagreement(
        self, client: Client, db: Any
    ) -> None:
        """The operator's own triage is learning material: ruling against the
        machine's staged verdict is the disagree signal, captured here because
        most rulings never get a feedback-widget click."""
        report = SecurityReportFactory(
            status="new",
            auto_triage_status="valid",
            triage_summary="The machine said this looks real.",
            assessed_severity="high",
        )

        response = client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "expected-behavior", "operator_notes": "Documented on purpose."},
        )

        assert response.status_code == 200
        feedback = SecurityTriageFeedback.objects.get(report=report)
        assert feedback.agreed is False
        assert feedback.operator_comment == "Documented on purpose."
        assert feedback.triage_summary_snapshot == "The machine said this looks real."
        report.refresh_from_db()
        assert report.auto_triage_status == ""

    def test_a_verdict_matching_the_staged_suggestion_records_agreement(
        self, client: Client, db: Any
    ) -> None:
        report = SecurityReportFactory(status="new", auto_triage_status="invalid")

        client.post(f"/security/{report.pk}/verdict/", {"status": "invalid"})

        feedback = SecurityTriageFeedback.objects.get(report=report)
        assert feedback.agreed is True

    def test_a_verdict_with_no_staged_suggestion_records_nothing(
        self, client: Client, db: Any
    ) -> None:
        """No machine verdict means nothing to agree or disagree with — the
        ruling itself still reaches the distiller as an operator ruling."""
        report = SecurityReportFactory(status="new")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "operator_notes": "Reviewed the code myself."},
        )

        assert not SecurityTriageFeedback.objects.filter(report=report).exists()


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

    def test_the_pending_counts_are_shown(self, client: Client, db: Any) -> None:
        """What the next distillation has to work with: feedback rows and the
        operator's own rulings."""
        SecurityTriageFeedback.objects.create(agreed=True, operator_comment="yes")
        SecurityReportFactory(status="valid")
        SecurityReportFactory(status="new")  # not a ruling

        body = client.get("/security/guidance/").content.decode()

        assert "1 feedback row" in body
        assert "1 operator ruling" in body


@pytest.mark.django_db
class TestSecurityGuidanceDistill:
    """The on-demand distill button on the guidance page."""

    @staticmethod
    def _config() -> Any:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig

        return OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_distills_global_and_per_project(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.core.models import SecurityTriageGuidance

        mock_config.return_value = self._config()
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, status="invalid", operator_notes="not exploitable")
        SecurityReportFactory(project=None, status="valid", operator_notes="real")

        with patch("franktheunicorn.review.backends.get_backend") as mock_get_backend:
            backend = MagicMock()
            backend.complete.return_value = "- Treat auth-disabled reports as invalid."
            mock_get_backend.return_value = backend
            response = client.post("/security/guidance/distill/")

        assert response.status_code == 302
        assert SecurityTriageGuidance.objects.filter(project=None).exists()
        guidance = SecurityTriageGuidance.objects.get(project=project)
        assert "auth-disabled" in guidance.guidance_text

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_nothing_to_learn_from_says_so(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        mock_config.return_value = self._config()

        response = client.post("/security/guidance/distill/", follow=True)

        assert response.status_code == 200
        assert "Nothing distilled" in response.content.decode()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_no_backend_is_an_error_message(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import OperatorConfig

        mock_config.return_value = OperatorConfig(github_username="testuser")

        response = client.post("/security/guidance/distill/", follow=True)

        assert response.status_code == 200
        assert "No LLM backend configured" in response.content.decode()


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

    def test_flipping_away_from_duplicate_drops_a_reference_nobody_resubmitted(
        self, client: Client
    ) -> None:
        """ "Actually valid" means the CVE it duplicated no longer describes it — but
        only when the operator didn't say otherwise in the same POST.

        This test used to submit the CVE field and still expect it cleared, which
        encoded the data loss a review caught: an operator typing "distinct bug,
        related to CVE-2026-2222" had it deleted on save, because the blanking rule
        ran after the assignment. Submitting the field is the operator's answer; the
        auto-drop is for the POST that carries none.
        """
        report = SecurityReportFactory(status="duplicate", matched_cve_id="CVE-2026-1234")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "operator_notes": ""},
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

    def test_the_undo_button_only_shows_when_there_is_a_snapshot(self, client: Client) -> None:
        """The button is the whole affordance — a press that does nothing teaches
        the operator to ignore it. So it renders only when an undo is real."""
        assert "Undo last import" not in client.get("/security/").content.decode()

        report = SecurityReportFactory(status="new")
        edited = self._csv(client).replace(",new,", ",invalid,")
        upload = SimpleUploadedFile("reviewed.csv", edited.encode(), content_type="text/csv")
        client.post("/security/import-csv/", {"csv_file": upload}, follow=True)
        report.refresh_from_db()
        assert report.status == "invalid"  # the import landed, so a snapshot exists

        assert "Undo last import" in client.get("/security/").content.decode()

    def test_undo_restores_the_state_before_the_import(self, client: Client) -> None:
        report = SecurityReportFactory(status="new", operator_notes="mine")
        edited = self._csv(client).replace(",new,", ",invalid,")
        upload = SimpleUploadedFile("reviewed.csv", edited.encode(), content_type="text/csv")
        client.post("/security/import-csv/", {"csv_file": upload}, follow=True)
        report.refresh_from_db()
        assert report.status == "invalid"

        client.post("/security/undo-import/", follow=True)

        report.refresh_from_db()
        assert report.status == "new"
        assert report.operator_notes == "mine"

    def test_undo_is_post_only(self, client: Client) -> None:
        assert client.get("/security/undo-import/").status_code == 405

    def test_undo_with_nothing_to_undo_is_a_no_op_flash(self, client: Client) -> None:
        response = client.post("/security/undo-import/", follow=True)
        assert "Nothing to undo" in response.content.decode()


@pytest.mark.django_db
class TestSecurityVerifyButton:
    """The Verify button and the per-branch verdict table.

    Every outcome is said out loud, including the gate declining — a button that
    swaps in nothing is indistinguishable from a broken one, which is the failure
    this codebase keeps writing rules about.
    """

    @staticmethod
    def _config(*, enabled: bool) -> Any:
        from franktheunicorn.config.models import OperatorConfig

        config = OperatorConfig()
        config.security_triage.verifier.enabled = enabled
        return config

    def test_the_button_is_on_the_report_page(self, client: Client) -> None:
        report = SecurityReportFactory(project=ProjectFactory())
        body = client.get(f"/security/{report.pk}/").content.decode()
        assert "Verify against the code" in body
        assert "Does the vulnerability actually exist?" in body
        assert "Verify versions" in body

    def test_clicking_queues_a_worker_command(self, client: Client) -> None:
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(project=ProjectFactory())
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=self._config(enabled=True),
        ):
            response = client.post(f"/security/{report.pk}/verify/")

        assert response.status_code == 200
        assert "queued" in response.content.decode().lower()
        assert (
            WorkerCommand.objects.filter(
                command="verify_security_report", security_report=report
            ).count()
            == 1
        )

    def test_a_second_click_does_not_queue_a_second_run(self, client: Client) -> None:
        """Minutes of agent time per branch, so a double-click mattering here is
        worse than for anything else in this queue."""
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(project=ProjectFactory())
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=self._config(enabled=True),
        ):
            client.post(f"/security/{report.pk}/verify/")
            response = client.post(f"/security/{report.pk}/verify/")

        assert "already running" in response.content.decode()
        assert WorkerCommand.objects.filter(command="verify_security_report").count() == 1

    def test_the_disabled_gate_names_the_setting(self, client: Client) -> None:
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(project=ProjectFactory())
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=self._config(enabled=False),
        ):
            response = client.post(f"/security/{report.pk}/verify/")

        body = response.content.decode()
        assert "verifier.enabled" in body
        assert WorkerCommand.objects.filter(command="verify_security_report").count() == 0

    def test_a_report_with_no_project_says_why_it_cannot_run(self, client: Client) -> None:
        report = SecurityReportFactory(project=None)
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=self._config(enabled=True),
        ):
            response = client.post(f"/security/{report.pk}/verify/")

        assert "no repository" in response.content.decode() or (
            "isn't attached" in response.content.decode()
        )

    def test_verify_is_post_only(self, client: Client) -> None:
        report = SecurityReportFactory()
        assert client.get(f"/security/{report.pk}/verify/").status_code == 405

    def test_per_branch_verdicts_are_shown(self, client: Client) -> None:
        """The whole point is comparing branches: real on master, fixed on 4.0,
        still shipping in 3.5 is three different pieces of news."""
        from franktheunicorn.core.models import SecurityVerification

        report = SecurityReportFactory(project=ProjectFactory())
        SecurityVerification.objects.create(
            report=report,
            branch="master",
            commit="deadbeefcafe1234",
            verdict="affected",
            confidence=0.88,
            summary="Reachable from the RPC handler with no auth.",
            evidence="core/rpc.py:88 — no auth check",
            agent="claude/opus",
        )
        SecurityVerification.objects.create(
            report=report, branch="branch-3.5", verdict="not-affected", summary="Fixed here."
        )

        body = client.get(f"/security/{report.pk}/").content.decode()

        assert "master" in body and "branch-3.5" in body
        assert "Affected" in body and "Not affected" in body
        assert "0.88" in body
        assert "core/rpc.py:88" in body
        assert "deadbeef" in body  # the commit, so the verdict has a shelf life
        assert "Re-verify" in body

    def test_raw_output_is_kept_visible_when_parsing_failed(self, client: Client) -> None:
        """So an unparseable answer can't pass for a considered one."""
        from franktheunicorn.core.models import SecurityVerification

        report = SecurityReportFactory(project=ProjectFactory())
        SecurityVerification.objects.create(
            report=report,
            branch="master",
            verdict="unclear",
            raw_output="I had a look and honestly I'm not sure.",
        )

        body = client.get(f"/security/{report.pk}/").content.decode()

        assert "no JSON verdict found" in body
        assert "honestly I&#x27;m not sure" in body or "honestly I'm not sure" in body

    def test_the_import_form_offers_verify_on_import(self, client: Client) -> None:
        body = client.get("/security/").content.decode()
        assert 'name="auto_verify"' in body
        assert "Verify against the code" in body
        assert 'name="auto_verify_versions"' in body
        assert "Verify versions" in body

    def test_map_versions_click_queues_a_worker_command(self, client: Client) -> None:
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(project=ProjectFactory())
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=self._config(enabled=True),
        ):
            response = client.post(f"/security/{report.pk}/map-versions/")

        assert response.status_code == 200
        assert b"Version mapping queued" in response.content
        assert (
            WorkerCommand.objects.filter(
                command="map_report_versions", security_report=report
            ).count()
            == 1
        )

    def test_map_versions_refuses_without_a_project(self, client: Client) -> None:
        report = SecurityReportFactory(project=None)
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=self._config(enabled=True),
        ):
            response = client.post(f"/security/{report.pk}/map-versions/")
        assert b"isn't attached" in response.content or b"no repository" in response.content


@pytest.mark.django_db
class TestVerdictCvePreservation:
    """The blanking rule ran after the form's own assignment, so a CVE typed in the
    same submission was deleted when the operator flipped a report off duplicate."""

    def test_a_cve_typed_while_leaving_duplicate_survives(self, client: Client) -> None:
        report = SecurityReportFactory(status="duplicate", matched_cve_id="CVE-2026-1111")

        client.post(
            f"/security/{report.pk}/verdict/",
            {
                "status": "valid",
                "operator_notes": "distinct bug, related to the other one",
                "matched_cve_id": "CVE-2026-2222",
            },
        )

        report.refresh_from_db()
        assert report.status == "valid"
        assert report.matched_cve_id == "CVE-2026-2222"

    def test_the_form_echoing_the_cve_back_unchanged_still_drops_it(self, client: Client) -> None:
        """The case the real UI produces, and the one the previous test missed.

        _security_verdict.html always renders the CVE input, so every dashboard POST
        carries the field. Keying the drop on field *presence* therefore made it dead
        code, and a duplicate's CVE stuck to a report just ruled valid — badge and
        all. The question is whether the operator CHANGED it, not whether the form
        sent it.
        """
        report = SecurityReportFactory(status="duplicate", matched_cve_id="CVE-2026-1111")

        # Exactly what the browser submits: the value the template rendered.
        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "operator_notes": "", "matched_cve_id": "CVE-2026-1111"},
        )

        report.refresh_from_db()
        assert report.matched_cve_id == ""

    def test_leaving_duplicate_without_the_field_still_drops_it(self, client: Client) -> None:
        """A POST with no CVE field at all (an htmx partial, a script)."""
        report = SecurityReportFactory(status="duplicate", matched_cve_id="CVE-2026-1111")

        client.post(f"/security/{report.pk}/verdict/", {"status": "valid"})

        report.refresh_from_db()
        assert report.matched_cve_id == ""

    def test_emptying_the_field_deliberately_still_works(self, client: Client) -> None:
        report = SecurityReportFactory(status="duplicate", matched_cve_id="CVE-2026-1111")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "matched_cve_id": ""},
        )

        report.refresh_from_db()
        assert report.matched_cve_id == ""


@pytest.mark.django_db
class TestSecurityReportAcceptTriage:
    """The Agree button promotes the staged auto_triage_status into status."""

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_accept_promotes_the_suggestion(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory(status="new", auto_triage_status="invalid")

        response = client.post(f"/security/{report.pk}/accept-triage/")

        assert response.status_code == 200
        report.refresh_from_db()
        assert report.status == "invalid"
        # The staging field is consumed.
        assert report.auto_triage_status == ""

    def test_accept_with_nothing_staged_says_so(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(status="new", auto_triage_status="")

        response = client.post(f"/security/{report.pk}/accept-triage/")

        assert response.status_code == 200
        assert b"No triage suggestion to accept" in response.content
        report.refresh_from_db()
        assert report.status == "new"

    def test_a_manual_verdict_clears_the_staged_suggestion(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(status="new", auto_triage_status="invalid")

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "operator_notes": "real after all"},
        )

        report.refresh_from_db()
        assert report.status == "valid"
        # The operator overruled the machine, so the stale suggestion is gone.
        assert report.auto_triage_status == ""

    def test_accept_records_agreement_for_the_guidance_loop(self, client: Client, db: Any) -> None:
        """An Agree click is agreement — the loop learns from it without waiting
        for a feedback-widget click that rarely comes."""
        report = SecurityReportFactory(
            status="new", auto_triage_status="invalid", triage_summary="Not exploitable here."
        )

        client.post(f"/security/{report.pk}/accept-triage/")

        feedback = SecurityTriageFeedback.objects.get(report=report)
        assert feedback.agreed is True
        assert feedback.triage_summary_snapshot == "Not exploitable here."


@pytest.mark.django_db
class TestSecurityReportRerunTriage:
    """The bulk re-triage button: procedural close, then LLM, then version
    follow-on for valid-looking ones. Skips operator-ruled and CVE-assigned."""

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_no_backend_says_so(self, mock_config: MagicMock, client: Client, db: Any) -> None:
        from franktheunicorn.config.models import OperatorConfig

        mock_config.return_value = OperatorConfig(github_username="testuser")
        response = client.post("/security/rerun-triage/")
        assert response.status_code == 302
        assert response["Location"].endswith("/security/")

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_procedural_close_runs_without_queuing_llm(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory(
            raw_text="If auth is off, the RPC handler accepts anything.", status="new"
        )

        client.post("/security/rerun-triage/")

        report.refresh_from_db()
        assert report.auto_triage_status == "invalid"
        # No LLM triage was queued — the cheap close handled it.
        assert not WorkerCommand.objects.filter(
            command="run_security_triage", security_report=report
        ).exists()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_valid_report_with_a_fix_branch_does_not_fan_out_the_verifier(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """The follow-on bills a coding-agent run per active release branch. Not
        gated on the general ruled test — a valid report *with* a CVE is what version
        mapping is for — but a recorded branch says the work is already done."""
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(
            project=project,
            status="valid",
            matched_cve_id="CVE-2026-4321",
            affected_versions="",
            fixed_in_branch="branch-3.5",
        )

        client.post("/security/rerun-triage/")

        assert not WorkerCommand.objects.filter(security_report=report).exists()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_report_with_a_recorded_fix_branch_is_left_alone(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """A branch is operator-typed, so the report has been worked on. Without it
        in the ruled test, re-run triage bills two LLM calls and stages a fresh
        suggestion over the operator's answer."""
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory(status="new", fixed_in_branch="branch-3.5")

        client.post("/security/rerun-triage/")

        report.refresh_from_db()
        assert not WorkerCommand.objects.filter(
            command="run_security_triage", security_report=report
        ).exists()
        assert report.auto_triage_status == ""

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_previously_triaged_report_with_auth_disabled_is_re_closed(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """The bug this fix is for: a report the LLM path already assessed and left
        in ``new`` with a staged verdict carries auth-disabled evidence in its own
        text. The never-been-triaged gate used to skip it (``retrigger=False``), so
        the full re-triage closed 0 where the standalone procedural button closed
        10. With ``retrigger=True`` the full re-triage reaches it too."""
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory(
            raw_text="If auth is off, the RPC handler accepts anything.",
            status="new",
            triage_summary="LLM said this is a real vuln.",
            auto_triage_status="valid",
        )

        client.post("/security/rerun-triage/")

        report.refresh_from_db()
        assert report.auto_triage_status == "invalid"
        assert not WorkerCommand.objects.filter(
            command="run_security_triage", security_report=report
        ).exists()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_report_with_triage_in_flight_is_not_re_closed(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """A triage run already under way owns the row. Re-closing it procedurally
        would race the worker — the close writes auto_triage_status="invalid", the
        in-flight LLM run overwrites it — so the cheap, evidence-based close loses
        to a guess. Skip it and let the run finish."""
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory(
            raw_text="If auth is off, the RPC handler accepts anything.",
            status="new",
            auto_triage_status="valid",
        )
        WorkerCommand.objects.create(
            command="run_security_triage", security_report=report, status="pending"
        )

        client.post("/security/rerun-triage/")

        report.refresh_from_db()
        # The in-flight run was not raced — the staged verdict stands.
        assert report.auto_triage_status == "valid"
        # Only the one command the test created; no second was queued.
        assert WorkerCommand.objects.filter(security_report=report).count() == 1

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_clean_report_queues_llm_triage(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory(raw_text="SQL injection in /api/users?id=1", status="new")

        client.post("/security/rerun-triage/")

        assert WorkerCommand.objects.filter(
            command="run_security_triage", security_report=report, status="pending"
        ).exists()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_operator_ruled_and_cve_reports_are_skipped(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        # status="new" but carries operator notes: reaches the loop (the
        # candidate filter admits "new") and is skipped by the notes guard, not
        # by the filter — so the in-loop skip is actually exercised.
        ruled = SecurityReportFactory(status="new", operator_notes="operator ruled")
        with_cve = SecurityReportFactory(status="new", matched_cve_id="CVE-2026-1")

        client.post("/security/rerun-triage/")

        # new + operator notes: skipped from re-triage by the notes guard.
        # new + a CVE: skipped from re-triage by the CVE guard.
        assert not WorkerCommand.objects.filter(security_report=ruled).exists()
        assert not WorkerCommand.objects.filter(security_report=with_cve).exists()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_valid_report_without_versions_queues_the_follow_on(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        report = SecurityReportFactory(status="valid", affected_versions="")

        client.post("/security/rerun-triage/")

        cmds = set(
            WorkerCommand.objects.filter(security_report=report).values_list("command", flat=True)
        )
        assert "map_report_versions" in cmds
        assert "verify_security_report" in cmds
        # Triage itself was not re-queued — the operator already ruled.
        assert "run_security_triage" not in cmds


@pytest.mark.django_db
class TestSecurityReportRerunTriageFailed:
    """The narrow re-run: only reports whose most recent triage command failed."""

    @staticmethod
    def _config() -> Any:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig

        return OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_failed_report_is_requeued_and_a_clean_one_is_not(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = self._config()
        failed = SecurityReportFactory(raw_text="SQL injection in /api/users?id=1", status="new")
        clean = SecurityReportFactory(raw_text="XSS in the admin UI", status="new")
        WorkerCommand.objects.create(
            command="run_security_triage",
            security_report=failed,
            status="failed",
            error="model unreachable",
            finished_at=timezone.now(),
        )
        WorkerCommand.objects.create(
            command="run_security_triage",
            security_report=clean,
            status="completed",
            finished_at=timezone.now(),
        )

        response = client.post("/security/rerun-triage-failed/")

        assert response.status_code == 302
        assert WorkerCommand.objects.filter(
            security_report=failed, command="run_security_triage", status="pending"
        ).exists()
        # The clean report got nothing — the full re-run's job, not this one's.
        assert not WorkerCommand.objects.filter(
            security_report=clean, command="run_security_triage", status="pending"
        ).exists()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_an_older_failure_under_a_newer_success_is_not_requeued(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        """The latest command is the one that counts: a report that failed once
        and then re-ran fine is not a failure."""
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = self._config()
        report = SecurityReportFactory(raw_text="SSRF in the webhook tester", status="new")
        WorkerCommand.objects.create(
            command="run_security_triage",
            security_report=report,
            status="failed",
            error="model unreachable",
            created_at=timezone.now() - timedelta(hours=2),
            finished_at=timezone.now() - timedelta(hours=2),
        )
        WorkerCommand.objects.create(
            command="run_security_triage",
            security_report=report,
            status="completed",
            created_at=timezone.now() - timedelta(hours=1),
            finished_at=timezone.now() - timedelta(hours=1),
        )

        client.post("/security/rerun-triage-failed/")

        assert not WorkerCommand.objects.filter(
            security_report=report, command="run_security_triage", status="pending"
        ).exists()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_report_ruled_since_the_failure_is_skipped(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = self._config()
        ruled = SecurityReportFactory(status="invalid", operator_notes="not a bug")
        WorkerCommand.objects.create(
            command="run_security_triage",
            security_report=ruled,
            status="failed",
            error="model unreachable",
            finished_at=timezone.now(),
        )

        client.post("/security/rerun-triage-failed/")

        assert not WorkerCommand.objects.filter(
            security_report=ruled, command="run_security_triage", status="pending"
        ).exists()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_no_backend_is_an_error_message(
        self, mock_config: MagicMock, client: Client, db: Any
    ) -> None:
        from franktheunicorn.config.models import OperatorConfig

        mock_config.return_value = OperatorConfig(github_username="testuser")

        response = client.post("/security/rerun-triage-failed/", follow=True)

        assert response.status_code == 200
        assert "No LLM backend configured" in response.content.decode()


@pytest.mark.django_db
class TestSecurityReportRerunProcedural:
    """The cheap-only re-trigger: re-run the auth-disabled regex close across
    the queue with zero LLM cost. Skips operator-ruled and in-flight reports,
    and re-closes already-triaged-but-not-ruled ones (the point of
    re-triggering)."""

    def test_a_report_the_close_missed_gets_closed(self, client: Client, db: Any) -> None:
        from franktheunicorn.core.models import WorkerCommand

        # Already triaged by the LLM (has a summary) but not operator-ruled —
        # the never-been-triaged gate would skip it, but re-trigger must not.
        report = SecurityReportFactory(
            raw_text="If auth is off, the RPC handler accepts anything.",
            status="new",
            triage_summary="LLM said this is a real vuln.",
            auto_triage_status="valid",
        )

        client.post("/security/rerun-procedural/")

        report.refresh_from_db()
        assert report.auto_triage_status == "invalid"
        # No LLM was queued — the cheap close handled it.
        assert not WorkerCommand.objects.filter(
            command="run_security_triage", security_report=report
        ).exists()

    def test_a_clean_report_is_left_alone(self, client: Client, db: Any) -> None:
        from franktheunicorn.core.models import WorkerCommand

        report = SecurityReportFactory(raw_text="SQL injection in /api/users?id=1", status="new")

        client.post("/security/rerun-procedural/")

        report.refresh_from_db()
        assert report.auto_triage_status == ""
        assert not WorkerCommand.objects.filter(security_report=report).exists()

    def test_operator_ruled_reports_are_skipped(self, client: Client, db: Any) -> None:
        from franktheunicorn.core.models import WorkerCommand

        # status="valid" is operator-ruled — excluded by the status="new" filter.
        ruled = SecurityReportFactory(status="valid", operator_notes="ruled")
        # new + a CVE — skipped by the CVE gate.
        with_cve = SecurityReportFactory(status="new", matched_cve_id="CVE-2026-1")
        # in-flight triaging — skipped (would race the worker).
        triaging = SecurityReportFactory(
            raw_text="If auth is off, bad things happen.", status="triaging"
        )

        client.post("/security/rerun-procedural/")

        assert not WorkerCommand.objects.filter(security_report=ruled).exists()
        assert not WorkerCommand.objects.filter(security_report=with_cve).exists()
        # The in-flight report was not re-closed.
        triaging.refresh_from_db()
        assert triaging.auto_triage_status == ""

    def test_it_works_with_no_llm_backend_configured(self, client: Client, db: Any) -> None:
        """The one bulk action that works with zero LLM — no backend check."""
        from franktheunicorn.config.models import OperatorConfig

        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=OperatorConfig(github_username="testuser"),  # no llm_backends
        ):
            report = SecurityReportFactory(
                raw_text="If auth is off, the RPC handler accepts anything.",
                status="new",
            )
            response = client.post("/security/rerun-procedural/")

        assert response.status_code == 302
        report.refresh_from_db()
        assert report.auto_triage_status == "invalid"


def _patch_duplicates_backend(response: str) -> Any:
    return patch(
        "franktheunicorn.security.triage.resolve_triage_backend",
        return_value=CannedLLMBackend(response),
    )


@pytest.mark.django_db
class TestSecurityReportRerunDuplicates:
    """The bulk duplicate re-check: one LLM title-grouping pass per project, links
    the groups it calls out and clears stale auto-links it saw both halves of and
    declined to group. Hand-set links are never touched."""

    def test_it_links_a_new_match_and_reports_both_halves(self, client: Client, db: Any) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        a = SecurityReportFactory(
            project=project, title="RCE in RPC", raw_text="port 7077 NettyRpcEnv"
        )
        b = SecurityReportFactory(
            project=project, title="RCE in RPC", raw_text="port 7077 NettyRpcEnv"
        )
        response_body = json.dumps(
            {"groups": [{"ids": [a.pk, b.pk], "confidence": "high", "reason": "same hole"}]}
        )

        with _patch_duplicates_backend(response_body):
            response = client.post("/security/rerun-duplicates/")

        assert response.status_code == 302
        b.refresh_from_db()
        assert b.duplicate_of_id == a.pk

    def test_it_clears_a_stale_auto_link(self, client: Client, db: Any) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        original = SecurityReportFactory(
            project=project, title="RCE in RPC", raw_text="port 7077 NettyRpcEnv"
        )
        stale = SecurityReportFactory(
            project=project,
            title="unrelated vuln",
            raw_text="nothing in common with the RPC report",
            duplicate_of=original,
            duplicate_confidence=1.0,
            duplicate_reason="same scanner finding id 'f005' in a different archive",
        )

        with _patch_duplicates_backend('{"groups": []}'):
            client.post("/security/rerun-duplicates/")

        stale.refresh_from_db()
        assert stale.duplicate_of_id is None
        assert stale.duplicate_confidence is None

    def test_a_hand_set_link_survives_the_re_check(self, client: Client, db: Any) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        original = SecurityReportFactory(
            project=project, title="RCE in RPC", raw_text="port 7077 NettyRpcEnv"
        )
        hand = SecurityReportFactory(
            project=project,
            title="hand linked",
            raw_text="operator decided this",
            duplicate_of=original,
            duplicate_confidence=None,
        )

        with _patch_duplicates_backend('{"groups": []}'):
            client.post("/security/rerun-duplicates/")

        hand.refresh_from_db()
        assert hand.duplicate_of_id == original.pk
        assert hand.duplicate_confidence is None

    def test_no_backend_is_an_error_message_not_a_silent_pass(
        self, client: Client, db: Any
    ) -> None:
        """A button that needs the model says so when there isn't one — "0 linked"
        from a check that never ran is the lie this codebase has a rule about."""
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, title="RCE in RPC", raw_text="port 7077 NettyRpcEnv")
        b = SecurityReportFactory(
            project=project, title="RCE in RPC", raw_text="port 7077 NettyRpcEnv"
        )

        with patch("franktheunicorn.security.triage.resolve_triage_backend", return_value=None):
            response = client.post("/security/rerun-duplicates/", follow=True)

        assert response.status_code == 200
        content = response.content.decode()
        assert "No LLM backend configured" in content
        b.refresh_from_db()
        assert b.duplicate_of_id is None


@pytest.mark.django_db
class TestSecurityReportVersions:
    """The explicit affected-versions field: copy from verification, or edit by hand."""

    def test_copy_seeds_from_the_rollup(self, client: Client, db: Any) -> None:
        from franktheunicorn.core.models import SecurityVerification

        report = SecurityReportFactory(affected_versions="")
        # The rollup reads each verification's version_impact (a list of
        # {name, status} rows), not the per-branch verdict — so seed those.
        SecurityVerification.objects.create(
            report=report,
            branch="master",
            verdict="affected",
            branch_order=0,
            version_impact=[{"name": "master", "status": "affected"}],
        )
        SecurityVerification.objects.create(
            report=report,
            branch="branch-3.5",
            verdict="not-affected",
            branch_order=1,
            version_impact=[{"name": "3.5.x", "status": "not-affected"}],
        )

        response = client.post(f"/security/{report.pk}/versions/", {"action": "copy"})

        assert response.status_code == 200
        report.refresh_from_db()
        # Only the affected row's release line is copied.
        assert "master" in report.affected_versions
        assert "3.5" not in report.affected_versions

    def test_copy_with_nothing_to_copy_says_so(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(affected_versions="")

        response = client.post(f"/security/{report.pk}/versions/", {"action": "copy"})

        assert response.status_code == 200
        assert b"No affected rows from verification to copy yet" in response.content
        report.refresh_from_db()
        assert report.affected_versions == ""

    def test_save_writes_the_operators_text(self, client: Client, db: Any) -> None:
        report = SecurityReportFactory(affected_versions="old")

        client.post(
            f"/security/{report.pk}/versions/",
            {"action": "save", "affected_versions": "3.5.0, 3.5.1, 4.0.0"},
        )

        report.refresh_from_db()
        assert report.affected_versions == "3.5.0, 3.5.1, 4.0.0"


@pytest.mark.django_db
class TestSecurityReportFix:
    """The one-click fix button and its refresh."""

    def test_fix_launches_the_agent(self, client: Client, db: Any) -> None:
        report = patched_report()
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
            patch(
                "franktheunicorn.security.fix_agent.remote_default_branch",
                return_value="master",
            ),
            patch(
                "franktheunicorn.security.fix_agent.httpx.post",
                return_value=cursor_response({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}}),
            ),
        ):
            response = client.post(f"/security/{report.pk}/fix/")

        assert response.status_code == 200
        assert b"bc-1" in response.content
        report.refresh_from_db()
        assert report.fix_status == "launched"

    def test_fix_without_a_patch_says_so(self, client: Client, db: Any) -> None:
        report = patched_report(proposed_patch="", proposed_patch_path="")
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
        ):
            response = client.post(f"/security/{report.pk}/fix/")

        assert response.status_code == 200
        assert b"no proposed patch" in response.content
        report.refresh_from_db()
        assert report.fix_status == ""

    def test_fix_without_an_api_key_names_the_env_var(self, client: Client, db: Any) -> None:
        report = patched_report()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
        ):
            response = client.post(f"/security/{report.pk}/fix/")

        assert b"CURSOR_API_KEY" in response.content

    def test_a_launched_report_refuses_a_second_agent(self, client: Client, db: Any) -> None:
        report = patched_report(fix_status="launched", fix_agent_id="bc-1", fix_run_id="run-1")
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
            patch("franktheunicorn.security.fix_agent.httpx.post") as mock_post,
        ):
            response = client.post(f"/security/{report.pk}/fix/")

        assert response.status_code == 200
        assert b"already launched" in response.content
        assert not mock_post.called

    def test_refresh_finds_the_branch(self, client: Client, db: Any) -> None:
        report = patched_report(fix_agent_id="bc-1", fix_run_id="run-1", fix_status="launched")
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
            patch(
                "franktheunicorn.security.fix_agent.httpx.get",
                return_value=cursor_response(
                    {
                        "status": "FINISHED",
                        "git": {
                            "branches": [
                                {
                                    "branch": "bug_86-quiet-cleanup",
                                    "repoUrl": (
                                        "https://github.com/holden/"
                                        + report.project.full_name.rsplit("/", 1)[-1]
                                    ),
                                }
                            ]
                        },
                    }
                ),
            ),
            patch(
                "franktheunicorn.security.fix_agent.find_fix_branches_on_fork",
                return_value=[],
            ),
        ):
            response = client.post(f"/security/{report.pk}/fix/refresh/")

        assert response.status_code == 200
        assert b"bug_86-quiet-cleanup" in response.content
        report.refresh_from_db()
        assert report.fix_status == "branch-pushed"


@pytest.mark.django_db
class TestSecurityRecheckFixed:
    """The bulk 'check untriaged against recent changes' button."""

    def test_launches_one_agent_per_project_and_queues_the_poll(
        self, client: Client, db: Any
    ) -> None:
        from franktheunicorn.core.models import SecurityRecheckRun, WorkerCommand

        one = SecurityReportFactory(status="new")
        other_project = ProjectFactory()
        two = SecurityReportFactory(status="new", project=other_project)
        SecurityReportFactory(status="valid")  # ruled on — not covered
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
            patch(
                "franktheunicorn.security.recheck.create_cursor_agent",
                return_value=("bc-x", "run-x"),
            ),
        ):
            response = client.post("/security/recheck-fixed/", follow=True)

        assert response.status_code == 200
        assert SecurityRecheckRun.objects.count() == 2
        assert WorkerCommand.objects.filter(command="poll_security_rechecks").exists()
        assert b"2 project(s)" in response.content
        # One run per project, each covering its one untriaged report.
        assert {r.report_count for r in SecurityRecheckRun.objects.all()} == {1}
        assert {r.project_id for r in SecurityRecheckRun.objects.all()} == {
            one.project_id,
            two.project_id,
        }

    def test_no_untriaged_reports_says_so(self, client: Client, db: Any) -> None:
        SecurityReportFactory(status="valid")
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
        ):
            response = client.post("/security/recheck-fixed/", follow=True)

        assert b"No untriaged reports with a project" in response.content

    def test_no_api_key_names_the_env_var(self, client: Client, db: Any) -> None:
        SecurityReportFactory(status="new")
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
        ):
            response = client.post("/security/recheck-fixed/", follow=True)

        assert b"CURSOR_API_KEY" in response.content

    def test_disabled_config_names_the_setting(self, client: Client, db: Any) -> None:
        SecurityReportFactory(status="new")
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=make_operator_config(enabled=False),
        ):
            response = client.post("/security/recheck-fixed/", follow=True)

        assert b"fix_agent.enabled" in response.content

    def test_a_second_press_does_not_double_queue_the_poll(self, client: Client, db: Any) -> None:
        from franktheunicorn.core.models import SecurityRecheckRun, WorkerCommand

        SecurityReportFactory(status="new")
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
            patch(
                "franktheunicorn.security.recheck.create_cursor_agent",
                return_value=("bc-x", "run-x"),
            ) as mock_create,
        ):
            client.post("/security/recheck-fixed/", follow=True)
            response = client.post("/security/recheck-fixed/", follow=True)

        # The first run is still in flight, so the second press launches
        # nothing — the running agent's prompt already covers these reports.
        assert mock_create.call_count == 1
        assert SecurityRecheckRun.objects.count() == 1
        assert b"already running" in response.content
        assert (
            WorkerCommand.objects.filter(command="poll_security_rechecks", status="pending").count()
            == 1
        )

    def test_a_stale_run_does_not_block_a_fresh_recheck(self, client: Client, db: Any) -> None:
        # A launched run older than the poll's own timeout outlived its poll;
        # it is not coming back, and it must not hold the button hostage.
        from franktheunicorn.core.models import SecurityRecheckRun

        report = SecurityReportFactory(status="new")
        stale = SecurityRecheckRunFactory(
            project=report.project,
            created_at=timezone.now() - timedelta(hours=2),
        )
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
            patch(
                "franktheunicorn.security.recheck.create_cursor_agent",
                return_value=("bc-new", "run-new"),
            ),
        ):
            response = client.post("/security/recheck-fixed/", follow=True)

        assert b"1 project(s)" in response.content
        stale.refresh_from_db()
        assert stale.status == "error"
        assert "stale" in stale.detail
        assert SecurityRecheckRun.objects.filter(status="launched").count() == 1

    def test_a_partial_chunk_failure_still_queues_the_poll(self, client: Client, db: Any) -> None:
        # Chunk 1 launched and is billing before chunk 2's POST fails; its run
        # still needs the poll, and the message must not claim nothing happened.
        from franktheunicorn.core.models import SecurityRecheckRun, WorkerCommand
        from franktheunicorn.security.fix_agent import FixAgentError

        project = ProjectFactory()
        for _ in range(51):  # two chunks
            SecurityReportFactory(status="new", project=project)
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=make_operator_config(),
            ),
            patch(
                "franktheunicorn.security.recheck.create_cursor_agent",
                side_effect=[("bc-1", "run-1"), FixAgentError("Cursor API said 500")],
            ),
        ):
            response = client.post("/security/recheck-fixed/", follow=True)

        assert SecurityRecheckRun.objects.count() == 1
        assert WorkerCommand.objects.filter(command="poll_security_rechecks").exists()
        assert b"did launch and will be polled" in response.content


class TestAnOrphanedRecheckRunIsPolledAgain:
    """A live run needs a poll even when this press launched nothing.

    A run whose poll command died — worker restart, budget spent — was never
    asked about again: it stayed launched, the view declined to queue a poll
    because nothing new had launched, and the stale sweep eventually binned it.
    Its verdicts were paid for and thrown away every time.
    """

    def test_a_live_run_with_no_new_launch_still_queues_a_poll(
        self, client: Client, db: Any
    ) -> None:
        import os

        from franktheunicorn.core.models import SecurityRecheckRun, WorkerCommand

        report = SecurityReportFactory(status="new")
        SecurityRecheckRun.objects.create(
            project=report.project, status="launched", report_count=1, chunk_index=0
        )
        WorkerCommand.objects.filter(command="poll_security_rechecks").delete()
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch("franktheunicorn.security.recheck.create_cursor_agent") as mock_create,
        ):
            response = client.post("/security/recheck-fixed/", follow=True)
        # Nothing new launched — that project already has a live run.
        assert not mock_create.called
        assert b"already running" in response.content
        # But the orphan is now going to be read.
        assert WorkerCommand.objects.filter(
            command="poll_security_rechecks", status="pending"
        ).exists()


class TestTheInjectionNoteReachesTheOperator:
    """``refuse_on_injection`` defaults false, so recording the hit IS the whole
    mitigation — and it was rendered only in the failed state, which is not the
    state anyone judging a pushed branch is looking at."""

    def _report_with_hits(self, status: str) -> Any:
        return patched_report(
            fix_agent_id="bc-1",
            fix_run_id="run-1",
            fix_status=status,
            fix_branch="bug_86-quiet" if status == "branch-pushed" else "",
            fix_injection_hits=["ignore_previous", "exfiltrate"],
        )

    def test_the_note_shows_on_a_pushed_branch(self, client: Client, db: Any) -> None:
        report = self._report_with_hits("branch-pushed")
        response = client.get(f"/security/{report.pk}/")
        assert response.status_code == 200
        assert b"prompt-injection patterns" in response.content
        assert b"ignore_previous" in response.content

    def test_the_note_shows_while_launched(self, client: Client, db: Any) -> None:
        report = self._report_with_hits("launched")
        response = client.get(f"/security/{report.pk}/")
        assert b"prompt-injection patterns" in response.content


class TestVerdictOverAnAutoTie:
    """Whose answer `fixed_in_branch` holds after the operator saves.

    `operator_has_ruled` reads `branch_match_applied` to decide a machine tie is
    not a ruling, so the flag has to track reality across the verdict form or the
    operator's own typing keeps not counting.
    """

    def _tied(self) -> Any:
        return SecurityReportFactory(
            status="new",
            fixed_in_branch="fix-cve-2025-12345",
            branch_match_branch="fix-cve-2025-12345",
            branch_match_applied=True,
        )

    def test_typing_a_different_branch_makes_it_the_operators(
        self, client: Client, db: Any
    ) -> None:
        report = self._tied()

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "valid", "fixed_in_branch": "branch-3.5", "operator_notes": ""},
        )

        report.refresh_from_db()
        assert report.fixed_in_branch == "branch-3.5"
        assert report.branch_match_applied is False
        assert report.operator_has_ruled is True

    def test_clearing_it_keeps_the_rejection_record(self, client: Client, db: Any) -> None:
        """The flag plus an empty field is the only record the sweep already
        offered this branch and was told no."""
        report = self._tied()

        client.post(
            f"/security/{report.pk}/verdict/",
            {"status": "new", "fixed_in_branch": "", "operator_notes": ""},
        )

        report.refresh_from_db()
        assert report.fixed_in_branch == ""
        assert report.branch_match_applied is True

    def test_saving_without_touching_the_branch_leaves_it_the_machines(
        self, client: Client, db: Any
    ) -> None:
        report = self._tied()

        client.post(
            f"/security/{report.pk}/verdict/",
            {
                "status": "new",
                "fixed_in_branch": "fix-cve-2025-12345",
                "operator_notes": "still looking",
            },
        )

        report.refresh_from_db()
        assert report.branch_match_applied is True
        # The notes are theirs, so this is ruled — just not via the branch.
        assert report.operator_has_ruled is True


class TestGitSweepButtons:
    """The two git-only sweeps: fetch origin, tie in branches / find the fixed ones.

    Both are worker commands, so the button's whole job is to queue exactly one
    and to say something true when it can't. The interesting cases are the
    can'ts: a press that queues a command the worker will no-op on is the
    "the button did nothing" failure this codebase keeps writing rules about.
    """

    def _operator(self) -> Any:
        from franktheunicorn.config.models import AgentCLIReviewerConfig, OperatorConfig

        config = OperatorConfig()
        config.agent_cli_reviewers = [AgentCLIReviewerConfig(name="claude", cli_path="claude")]
        return config

    def _queued(self, command: str) -> Any:
        from franktheunicorn.core.models import WorkerCommand

        return WorkerCommand.objects.filter(command=command)

    def _post(self, client: Client, path: str, operator: Any = None) -> Any:
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=operator or self._operator(),
        ):
            return client.post(path, follow=True)

    def test_the_branch_sweep_queues_one_command(self, client: Client, db: Any) -> None:
        SecurityReportFactory(status="new")

        response = self._post(client, "/security/match-branches/")

        assert response.status_code == 200
        assert self._queued("match_security_branches").filter(status="pending").count() == 1

    def test_a_second_press_does_not_queue_a_second_sweep(self, client: Client, db: Any) -> None:
        SecurityReportFactory(status="new")

        self._post(client, "/security/match-branches/")
        response = self._post(client, "/security/match-branches/")

        assert b"already queued or running" in response.content
        assert self._queued("match_security_branches").count() == 1

    def test_the_sweeps_go_in_at_bulk_priority(self, client: Client, db: Any) -> None:
        """A 300-branch walk in the interactive lane parks the lane that exists
        so a click doesn't wait behind bulk work."""
        from franktheunicorn.security.queue import PRIORITY_INTERACTIVE

        SecurityReportFactory(status="new")
        self._post(client, "/security/match-branches/")

        assert self._queued("match_security_branches").get().priority < PRIORITY_INTERACTIVE

    def test_a_project_less_backlog_says_so_rather_than_queueing(
        self, client: Client, db: Any
    ) -> None:
        SecurityReportFactory(project=None, status="new")

        response = self._post(client, "/security/match-branches/")

        assert b"needs a project" in response.content
        assert not self._queued("match_security_branches").exists()

    def test_no_reviewer_configured_names_the_setting(self, client: Client, db: Any) -> None:
        from franktheunicorn.config.models import OperatorConfig

        SecurityReportFactory(status="new")
        bare = OperatorConfig()
        bare.agent_cli_reviewers = []

        response = self._post(client, "/security/match-branches/", bare)

        assert b"agent_cli_reviewers" in response.content
        assert not self._queued("match_security_branches").exists()

    def test_the_fixed_sweep_queues_one_command(self, client: Client, db: Any) -> None:
        SecurityReportFactory(status="new", proposed_patch="--- a/x\n+++ b/x\n")

        response = self._post(client, "/security/scan-fixed/")

        assert b"1 report(s) with a proposed patch" in response.content
        assert self._queued("scan_security_fixed").filter(status="pending").count() == 1

    def test_no_patch_in_the_backlog_points_at_the_button_that_can(
        self, client: Client, db: Any
    ) -> None:
        """Reverse-applying a patch is the whole trick; without one, git has
        nothing to say and the cloud recheck is the answer."""
        SecurityReportFactory(status="new", proposed_patch="")

        response = self._post(client, "/security/scan-fixed/")

        assert b"Check Untriaged vs Recent Changes" in response.content
        assert not self._queued("scan_security_fixed").exists()

    def test_the_two_sweeps_do_not_block_each_other(self, client: Client, db: Any) -> None:
        SecurityReportFactory(status="new", proposed_patch="--- a/x\n+++ b/x\n")

        self._post(client, "/security/match-branches/")
        self._post(client, "/security/scan-fixed/")

        assert self._queued("match_security_branches").count() == 1
        assert self._queued("scan_security_fixed").count() == 1

    def test_both_buttons_are_on_the_page(self, client: Client, db: Any) -> None:
        response = client.get("/security/")
        assert b"/security/match-branches/" in response.content
        assert b"/security/scan-fixed/" in response.content
