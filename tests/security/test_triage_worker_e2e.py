"""End-to-end: pasted report -> queued command -> worker -> triaged report.

Covers the seam the unit tests mock out — the dashboard queues a
WorkerCommand, the worker claims it, and triage_report actually runs — using
the stub LLM backend so no network or API key is involved.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client

from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
from franktheunicorn.core.models import Project, SecurityReport, WorkerCommand
from franktheunicorn.worker.commands import process_pending_commands
from tests.factories import ProjectFactory, SecurityReportFactory

_VALID_ANALYSIS = {
    "poc_plausible": True,
    "poc_assessment": "The parameter is echoed into the page unescaped.",
    "is_expected_behavior": False,
    "expected_behavior_explanation": "",
    "triage_summary": "Reflected XSS in the History Server log page.",
    "assessed_severity": "medium",
}


def _operator_config() -> OperatorConfig:
    config = OperatorConfig(
        github_username="holdenk",
        llm_backends=[LLMBackendConfig(provider="stub")],
    )
    config.security_triage.enabled = True
    config.security_triage.auto_triage = True
    return config


@pytest.fixture
def no_cve_lookup() -> Any:
    """Keep NVD out of it — triage must not need the network to complete."""
    with patch("franktheunicorn.security.triage.search_cves", return_value=[]) as m:
        yield m


@pytest.mark.django_db
class TestPasteToTriaged:
    def _paste(self, client: Client, project: Project, config: OperatorConfig) -> SecurityReport:
        with patch("franktheunicorn.config.loader.get_operator_config", return_value=config):
            response = client.post(
                "/security/new/",
                {
                    "title": "XSS in the History Server log page",
                    "raw_text": "Reflected XSS via the ?logPage= parameter.",
                    "project": str(project.pk),
                    "reporter_email": "reporter@example.com",
                },
            )
        assert response.status_code == 302
        return SecurityReport.objects.get(title__startswith="XSS in")

    def test_paste_queues_and_worker_triages(
        self, client: Client, no_cve_lookup: Any, tmp_path: Any
    ) -> None:
        config = _operator_config()
        project = ProjectFactory(owner="apache", repo="spark")
        report = self._paste(client, project, config)

        cmd = WorkerCommand.objects.get(command="run_security_triage", security_report=report)
        assert cmd.status == "pending"
        # The web request must not have done the triage itself.
        assert report.status == "new"

        with patch(
            "franktheunicorn.security.triage._call_llm",
            return_value=dict(_VALID_ANALYSIS),
        ):
            assert process_pending_commands(config) == 1

        cmd.refresh_from_db()
        report.refresh_from_db()
        assert cmd.status == "completed", cmd.error
        assert report.assessed_severity == "medium"
        assert report.poc_plausible is True
        assert "Reflected XSS" in report.triage_summary
        assert "severity='medium'" in cmd.log

    def test_unparseable_llm_output_does_not_strand_the_report(
        self, client: Client, no_cve_lookup: Any
    ) -> None:
        """The stub returns prose, not JSON — the report has to stay visible.

        "triaging" is not shown in the new-reports queue, so a report left in
        that state is silently lost.
        """
        config = _operator_config()
        project = ProjectFactory(owner="apache", repo="spark")
        report = self._paste(client, project, config)

        assert process_pending_commands(config) == 1

        report.refresh_from_db()
        assert report.status == "new"

    def test_llm_exception_does_not_strand_the_report(
        self, client: Client, no_cve_lookup: Any
    ) -> None:
        config = _operator_config()
        project = ProjectFactory(owner="apache", repo="spark")
        report = self._paste(client, project, config)

        with patch(
            "franktheunicorn.security.triage._call_llm",
            side_effect=RuntimeError("model timed out"),
        ):
            assert process_pending_commands(config) == 1

        report.refresh_from_db()
        assert report.status == "new"

    def test_operator_verdict_survives_a_failed_retriage(
        self, client: Client, no_cve_lookup: Any
    ) -> None:
        """A report the operator already ruled on must not be reset to new."""
        from franktheunicorn.security.triage import triage_report

        config = _operator_config()
        report = SecurityReportFactory(title="already judged", raw_text="...")
        SecurityReport.objects.filter(pk=report.pk).update(status="valid")
        report.refresh_from_db()

        with patch(
            "franktheunicorn.security.triage._call_llm",
            side_effect=RuntimeError("model timed out"),
        ):
            triage_report(report, None, config)

        report.refresh_from_db()
        assert report.status == "valid"


@pytest.mark.django_db
class TestRetriageOfJudgedReports:
    """Re-triage has to be able to revise triage's own past verdicts."""

    def test_expected_behavior_report_can_be_retriaged(self, no_cve_lookup: Any) -> None:
        """'expected-behavior' is set BY triage, so triage must be able to undo it.

        Leaving it out of the auto-managed set froze such reports: the new
        verdict was computed and saved to every other field, then never
        surfaced in any queue.
        """
        from franktheunicorn.security.triage import triage_report

        config = _operator_config()
        report = SecurityReportFactory(title="reconsider me", raw_text="...")
        SecurityReport.objects.filter(pk=report.pk).update(status="expected-behavior")
        report.refresh_from_db()

        revised = dict(_VALID_ANALYSIS, is_expected_behavior=False, assessed_severity="high")
        with patch("franktheunicorn.security.triage._call_llm", return_value=revised):
            triage_report(report, None, config)

        report.refresh_from_db()
        assert report.status == "new"
        assert report.assessed_severity == "high"

    def test_operator_verdict_set_mid_run_is_not_clobbered(self, no_cve_lookup: Any) -> None:
        """Triage is async now, so a verdict can land while the LLM is thinking."""
        from franktheunicorn.security.triage import triage_report

        config = _operator_config()
        report = SecurityReportFactory(title="racy", raw_text="...")

        def analyse_then_operator_rules(*args: Any, **kwargs: Any) -> dict[str, Any]:
            # Stand in for the operator clicking a verdict during the call.
            SecurityReport.objects.filter(pk=report.pk).update(status="valid")
            return dict(_VALID_ANALYSIS)

        with patch(
            "franktheunicorn.security.triage._call_llm",
            side_effect=analyse_then_operator_rules,
        ):
            triage_report(report, None, config)

        report.refresh_from_db()
        assert report.status == "valid", "operator's verdict was overwritten by the worker"
        # The analysis itself is still recorded.
        assert report.triage_summary


@pytest.mark.django_db
class TestEmailIngestionQueuesTriage:
    def test_email_report_is_queued_not_triaged_inline(self) -> None:
        """The email door has to behave like the others: queue, don't run.

        Inline triage blocked the poll cycle on an NVD lookup plus two LLM
        calls per report, skipped the in-flight dedup (so a later operator
        click ran the whole thing again), and hardcoded project_config=None
        instead of letting the command handler resolve the project's security
        model.
        """
        from datetime import UTC, datetime

        from franktheunicorn.core.models import WorkerCommand
        from franktheunicorn.worker import runner

        config = _operator_config()
        config.security_triage.email.enabled = True

        message = MagicMock(
            message_id="<m1>",
            subject="[SECURITY] path traversal",
            from_name="Reporter",
            from_email="r@example.com",
            body="A path traversal in the log viewer.",
            received_at=datetime(2026, 8, 1, tzinfo=UTC),
            is_forwarded=False,
            matched_keywords=["path traversal"],
            is_security_report=True,
        )
        fetched = MagicMock(examined=[message], already_scanned=0)

        with (
            patch("franktheunicorn.security.triage.triage_report") as mock_triage,
            patch(
                "franktheunicorn.data_access.email_inbox.fetcher.fetch_security_emails",
                return_value=fetched,
            ),
        ):
            runner._last_security_email_poll = 0.0
            runner._poll_security_emails(config)

        report = SecurityReport.objects.get(email_message_id="<m1>")
        mock_triage.assert_not_called()
        assert WorkerCommand.objects.filter(
            command="run_security_triage", security_report=report, status="pending"
        ).exists()
