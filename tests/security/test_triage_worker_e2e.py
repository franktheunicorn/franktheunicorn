"""End-to-end: pasted report -> queued command -> worker -> triaged report.

Covers the seam the unit tests mock out — the dashboard queues a
WorkerCommand, the worker claims it, and triage_report actually runs — using
the stub LLM backend so no network or API key is involved.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.test import Client

from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
from franktheunicorn.core.models import Project, SecurityReport, WorkerCommand
from franktheunicorn.worker.commands import process_pending_commands

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
        project = Project.objects.create(owner="apache", repo="spark")
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
        project = Project.objects.create(owner="apache", repo="spark")
        report = self._paste(client, project, config)

        assert process_pending_commands(config) == 1

        report.refresh_from_db()
        assert report.status == "new"

    def test_llm_exception_does_not_strand_the_report(
        self, client: Client, no_cve_lookup: Any
    ) -> None:
        config = _operator_config()
        project = Project.objects.create(owner="apache", repo="spark")
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
        report = SecurityReport.objects.create(title="already judged", raw_text="...")
        SecurityReport.objects.filter(pk=report.pk).update(status="valid")
        report.refresh_from_db()

        with patch(
            "franktheunicorn.security.triage._call_llm",
            side_effect=RuntimeError("model timed out"),
        ):
            triage_report(report, None, config)

        report.refresh_from_db()
        assert report.status == "valid"
