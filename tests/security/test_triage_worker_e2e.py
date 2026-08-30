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

        from franktheunicorn.security.triage import TriageIncompleteError

        # A dead model produces no verdict, which now surfaces as TriageIncompleteError
        # so the WorkerCommand is marked failed instead of completed. The
        # operator's verdict must survive that untouched.
        with (
            patch(
                "franktheunicorn.security.triage._call_llm",
                side_effect=RuntimeError("model timed out"),
            ),
            pytest.raises(TriageIncompleteError),
        ):
            triage_report(report, None, config)

        report.refresh_from_db()
        assert report.status == "valid"


@pytest.mark.django_db
class TestRetriageOfJudgedReports:
    """Re-triage has to be able to revise triage's own past verdicts."""

    def test_expected_behavior_report_can_be_retriaged(self, no_cve_lookup: Any) -> None:
        """Under the staging design the machine writes its verdict to
        ``auto_triage_status`` and leaves ``status`` alone, so an operator-set
        ``expected-behavior`` is preserved across a re-triage and the revised
        verdict is staged for an Agree click — rather than the old failure
        where a machine-set expected-behavior froze because nothing could
        take it back.
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
        # The operator's verdict stands; the revised assessment is staged.
        assert report.status == "expected-behavior"
        assert report.auto_triage_status == "valid"
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
            # -inf, not 0.0. The poll gate is
            # `time.monotonic() - _last_security_email_poll < poll_interval_seconds`,
            # and monotonic() is *seconds since boot*: on a developer box that has
            # been up for days it is ~460,000 and 0.0 looks infinitely stale, but on
            # a freshly-booted CI runner it is double digits, so 0.0 - with a 300s
            # default interval - meant the poll returned early, no report was
            # created, and the test died on DoesNotExist. Reproduced by pinning
            # monotonic() to 120.0. tests/worker/test_security_email_poll.py already
            # used -inf for this reason; this copy did not.
            runner._last_security_email_poll = float("-inf")
            runner._poll_security_emails(config)

        report = SecurityReport.objects.get(email_message_id="<m1>")
        mock_triage.assert_not_called()
        assert WorkerCommand.objects.filter(
            command="run_security_triage", security_report=report, status="pending"
        ).exists()


@pytest.mark.django_db
class TestTriageChainsVersionFollowOn:
    """A triage run that rules a report valid-looking fans out to the cheap
    version map and the deep verifier — but only for valid-looking verdicts,
    and only when the verifier gate and a project let it run."""

    def test_a_valid_verdict_queues_version_map_and_verify(self, no_cve_lookup: Any) -> None:
        from franktheunicorn.security.queue import queue_triage
        from franktheunicorn.worker.commands import process_pending_commands

        config = _operator_config()
        # A report with a project, so the follow-on has a repo to check against.
        report = SecurityReportFactory(
            title="real hole", raw_text="...", parsed_component="core/src/main/scala/Foo.scala"
        )
        assert report.project_id is not None

        queue_triage(report)
        with patch(
            "franktheunicorn.security.triage._call_llm",
            return_value=dict(_VALID_ANALYSIS),
        ):
            # limit=1: process only the triage, leaving the follow-on commands
            # pending so this test isn't also a version-map/verify run.
            assert process_pending_commands(config, limit=1) == 1

        report.refresh_from_db()
        assert report.auto_triage_status == "valid"
        cmds = set(
            WorkerCommand.objects.filter(security_report=report).values_list("command", flat=True)
        )
        assert "run_security_triage" in cmds
        assert "map_report_versions" in cmds
        assert "verify_security_report" in cmds

    def test_an_invalid_verdict_does_not_queue_the_follow_on(self, no_cve_lookup: Any) -> None:
        from franktheunicorn.security.queue import queue_triage
        from franktheunicorn.worker.commands import process_pending_commands

        config = _operator_config()
        report = SecurityReportFactory(
            title="not a hole", raw_text="...", parsed_component="core/Foo.scala"
        )
        queue_triage(report)
        invalid = dict(_VALID_ANALYSIS, poc_plausible=False, is_expected_behavior=False)
        with patch("franktheunicorn.security.triage._call_llm", return_value=invalid):
            assert process_pending_commands(config, limit=1) == 1

        report.refresh_from_db()
        assert report.auto_triage_status == "invalid"
        # No follow-on was queued: nothing to map for a report triage called invalid.
        cmds = set(
            WorkerCommand.objects.filter(security_report=report).values_list("command", flat=True)
        )
        assert cmds == {"run_security_triage"}

    def test_an_expected_behavior_verdict_does_not_queue_the_follow_on(
        self, no_cve_lookup: Any
    ) -> None:
        from franktheunicorn.security.queue import queue_triage
        from franktheunicorn.worker.commands import process_pending_commands

        config = _operator_config()
        report = SecurityReportFactory(title="documented", raw_text="...")
        queue_triage(report)
        expected = dict(_VALID_ANALYSIS, is_expected_behavior=True)
        with patch("franktheunicorn.security.triage._call_llm", return_value=expected):
            assert process_pending_commands(config, limit=1) == 1

        report.refresh_from_db()
        assert report.auto_triage_status == "expected-behavior"
        cmds = set(
            WorkerCommand.objects.filter(security_report=report).values_list("command", flat=True)
        )
        assert cmds == {"run_security_triage"}

    def test_an_operator_invalid_verdict_mid_run_is_not_overruled(self, no_cve_lookup: Any) -> None:
        """The operator rules invalid from the detail page while the LLM is
        thinking and says valid. The machine must NOT re-stage a contradicting
        suggestion (no Agree banner offering to flip it back) and must NOT bill
        the version/verify follow-on on a report the operator ruled not-a-vuln.
        """
        from franktheunicorn.security.queue import queue_triage
        from franktheunicorn.worker.commands import process_pending_commands

        config = _operator_config()
        report = SecurityReportFactory(title="contested", raw_text="...")
        queue_triage(report)

        def rule_invalid_then_say_valid(*args: Any, **kwargs: Any) -> dict[str, Any]:
            # Stand in for the operator clicking "invalid" on the verdict form,
            # which sets status and clears the staged suggestion.
            SecurityReport.objects.filter(pk=report.pk).update(
                status="invalid", auto_triage_status=""
            )
            return dict(_VALID_ANALYSIS)  # the machine says valid

        with patch(
            "franktheunicorn.security.triage._call_llm",
            side_effect=rule_invalid_then_say_valid,
        ):
            assert process_pending_commands(config, limit=1) == 1

        report.refresh_from_db()
        # The operator's verdict stands...
        assert report.status == "invalid"
        # ...and no contradicting suggestion was staged.
        assert report.auto_triage_status == ""
        # ...and no follow-on was billed on a report ruled not-a-vuln.
        cmds = set(
            WorkerCommand.objects.filter(security_report=report).values_list("command", flat=True)
        )
        assert cmds == {"run_security_triage"}
