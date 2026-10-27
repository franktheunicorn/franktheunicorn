"""Tests for the CVE-match dashboard views (security_cve_match and friends)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import Client

from franktheunicorn.config.models import (
    AgentCLIReviewerConfig,
    LLMBackendConfig,
    OperatorConfig,
    SecurityCveMatchConfig,
    SecurityTriageConfig,
)
from franktheunicorn.core.models import TentativeCveMatch
from tests.factories import ProjectFactory, SecurityReportFactory


def _operator_config(*, reviewer_configured: bool = True) -> OperatorConfig:
    config = OperatorConfig(
        github_username="holden", llm_backends=[LLMBackendConfig(provider="stub")]
    )
    config.security_triage = SecurityTriageConfig(enabled=True)
    config.security_triage.cve_match = SecurityCveMatchConfig(reviewer="claude")
    # The default seeds a `claude` entry (enabled="auto"), so "no reviewer
    # configured" has to empty the list explicitly to simulate an operator who
    # removed it.
    config.agent_cli_reviewers = (
        [AgentCLIReviewerConfig(name="claude", cli_path="claude")] if reviewer_configured else []
    )
    return config


@pytest.mark.django_db
class TestCveMatchGet:
    def test_renders_the_form_and_empty_state(self, client: Client) -> None:
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=_operator_config(),
        ):
            response = client.get("/security/cve-match/")
        assert response.status_code == 200
        assert b"CVE Match" in response.content
        assert b"No CVEs pasted yet" in response.content

    def test_disables_the_match_button_when_no_reviewer(self, client: Client) -> None:
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=_operator_config(reviewer_configured=False),
        ):
            response = client.get("/security/cve-match/")
        assert b"disabled" in response.content
        assert b"Matching is disabled" in response.content


@pytest.mark.django_db
class TestCveMatchPost:
    def test_requires_text(self, client: Client) -> None:
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=_operator_config(),
        ):
            response = client.post("/security/cve-match/", {"raw_text": "", "project_id": "1"})
        assert response.status_code == 302
        # No row created.
        assert TentativeCveMatch.objects.count() == 0

    def test_requires_a_project(self, client: Client) -> None:
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=_operator_config(),
        ):
            response = client.post(
                "/security/cve-match/", {"raw_text": "CVE-2026-1: x", "project_id": ""}
            )
        assert response.status_code == 302
        assert TentativeCveMatch.objects.count() == 0

    def test_rejects_a_paste_with_no_cve_ids(self, client: Client) -> None:
        project = ProjectFactory()
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=_operator_config(),
        ):
            client.post(
                "/security/cve-match/",
                {"raw_text": "just some prose, no CVE", "project_id": str(project.pk)},
            )
        assert TentativeCveMatch.objects.count() == 0

    def test_upserts_and_queues_the_matcher(self, client: Client) -> None:
        project = ProjectFactory()
        with (
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=_operator_config(),
            ),
            patch("franktheunicorn.security.queue.queue_cve_match", return_value=True) as queued,
        ):
            response = client.post(
                "/security/cve-match/",
                {
                    "raw_text": "CVE-2026-12345: RCE in foo\nCVE-2026-00002: DoS",
                    "project_id": str(project.pk),
                },
            )
        assert response.status_code == 302
        rows = list(TentativeCveMatch.objects.filter(project=project).order_by("cve_id"))
        assert [r.cve_id for r in rows] == ["CVE-2026-00002", "CVE-2026-12345"]
        assert all(r.status == "pending" for r in rows)
        queued.assert_called_once_with(project)

    def test_no_pending_rows_does_not_queue(self, client: Client) -> None:
        project = ProjectFactory()
        # Pre-create a confirmed row so the re-paste finds nothing to re-run.
        report = SecurityReportFactory(project=project)
        TentativeCveMatch.objects.create(
            project=project, cve_id="CVE-2026-12345", status="confirmed", report=report
        )
        with (
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=_operator_config(),
            ),
            patch("franktheunicorn.security.queue.queue_cve_match", return_value=True) as queued,
        ):
            client.post(
                "/security/cve-match/",
                {"raw_text": "CVE-2026-12345: same one", "project_id": str(project.pk)},
            )
        queued.assert_not_called()

    def test_warns_when_no_reviewer_configured(self, client: Client) -> None:
        project = ProjectFactory()
        with (
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=_operator_config(reviewer_configured=False),
            ),
            patch("franktheunicorn.security.queue.queue_cve_match") as queued,
        ):
            client.post(
                "/security/cve-match/",
                {"raw_text": "CVE-2026-12345: x", "project_id": str(project.pk)},
            )
        # The row is still saved (so the operator can re-run after configuring).
        assert TentativeCveMatch.objects.filter(project=project).count() == 1
        queued.assert_not_called()


@pytest.mark.django_db
class TestCveMatchConfirm:
    def test_writes_cve_onto_report_and_marks_confirmed(self, client: Client) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project)
        match = TentativeCveMatch.objects.create(
            project=project, cve_id="CVE-2026-1", status="tentative", report=report
        )
        client.post(f"/security/cve-match/{match.pk}/confirm/")
        report.refresh_from_db()
        match.refresh_from_db()
        assert report.matched_cve_id == "CVE-2026-1"
        assert match.status == "confirmed"

    def test_a_match_with_no_report_is_refused(self, client: Client) -> None:
        project = ProjectFactory()
        match = TentativeCveMatch.objects.create(
            project=project, cve_id="CVE-2026-1", status="pending"
        )
        client.post(f"/security/cve-match/{match.pk}/confirm/")
        match.refresh_from_db()
        assert match.status == "pending"  # untouched


@pytest.mark.django_db
class TestCveMatchReject:
    def test_marks_rejected(self, client: Client) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project)
        match = TentativeCveMatch.objects.create(
            project=project, cve_id="CVE-2026-1", status="tentative", report=report
        )
        client.post(f"/security/cve-match/{match.pk}/reject/")
        match.refresh_from_db()
        assert match.status == "rejected"
        # The report is untouched.
        report.refresh_from_db()
        assert report.matched_cve_id == ""


@pytest.mark.django_db
class TestCveMatchRerun:
    def test_marks_pending_and_queues(self, client: Client) -> None:
        project = ProjectFactory()
        match = TentativeCveMatch.objects.create(
            project=project, cve_id="CVE-2026-12345", status="no_match"
        )
        with (
            patch(
                "franktheunicorn.config.loader.get_operator_config",
                return_value=_operator_config(),
            ),
            patch("franktheunicorn.security.queue.queue_cve_match", return_value=True) as queued,
        ):
            client.post(f"/security/cve-match/{match.pk}/rerun/")
        match.refresh_from_db()
        assert match.status == "pending"
        assert match.report_id is None
        queued.assert_called_once_with(project)
