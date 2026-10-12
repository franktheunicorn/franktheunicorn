"""The security page's agent scope: labelling imports, filtering, and scoping every button.

Another agent's findings are a different kind of input from a person's report.
These tests pin the three promises the page makes about that: an import can say
which agent produced it, the list can be narrowed to one source (or every agent,
or none), and every bulk button acts on exactly the source the page is showing —
never more, even when handed a scope it can't read.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from franktheunicorn.core.models import SecurityRecheckRun, SecurityReport, WorkerCommand
from tests.factories import ProjectFactory, SecurityReportFactory, make_operator_config

REPORT_TEXT = (
    "There is a path traversal vulnerability in the archive extractor.\n"
    "Passing ../../etc/passwd as a member name writes outside the target dir.\n"
)

#: Text the procedural close recognises: an attack that only works with auth off.
AUTH_OFF_TEXT = "If auth is off, the RPC handler accepts anything."


def _verifier_operator() -> Any:
    """An operator config the verifier gate passes: a reviewer to run."""
    from franktheunicorn.config.models import AgentCLIReviewerConfig, OperatorConfig

    config = OperatorConfig()
    config.agent_cli_reviewers = [AgentCLIReviewerConfig(name="claude", cli_path="claude")]
    return config


def _triage_operator() -> Any:
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig

    return OperatorConfig(
        github_username="testuser", llm_backends=[LLMBackendConfig(provider="stub")]
    )


def _post(client: Client, path: str, operator: Any, **data: str) -> Any:
    with patch("franktheunicorn.config.loader.get_operator_config", return_value=operator):
        return client.post(path, data, follow=True)


def _messages(response: Any) -> list[str]:
    return [str(message) for message in response.context["messages"]]


@pytest.mark.django_db
class TestListScope:
    def _backlog(self) -> dict[str, SecurityReport]:
        return {
            "human": SecurityReportFactory(title="Person-sent traversal", source_agent=""),
            "xbow": SecurityReportFactory(title="Scanner finding one", source_agent="xbow"),
            "xbow_valid": SecurityReportFactory(
                title="Scanner finding two", source_agent="xbow", status="valid"
            ),
            "claude": SecurityReportFactory(title="Claude finding", source_agent="claude-code"),
        }

    def test_one_agent_narrows_the_rows_and_the_tab_counts(self, client: Client) -> None:
        self._backlog()

        response = client.get("/security/?agent=xbow")

        body = response.content.decode()
        assert "Scanner finding one" in body
        assert "Scanner finding two" in body
        assert "Person-sent traversal" not in body
        assert "Claude finding" not in body
        counts = {tab["key"]: tab["count"] for tab in response.context["status_tabs"]}
        # Counted inside the scope, or the badge can't be accounted for by the rows.
        assert counts["all"] == 2
        assert counts["valid"] == 1

    def test_none_is_the_reports_people_sent(self, client: Client) -> None:
        self._backlog()

        body = client.get("/security/?agent=none").content.decode()

        assert "Person-sent traversal" in body
        assert "Scanner finding one" not in body

    def test_any_is_every_agents_reports(self, client: Client) -> None:
        self._backlog()

        body = client.get("/security/?agent=any").content.decode()

        assert "Claude finding" in body
        assert "Scanner finding one" in body
        assert "Person-sent traversal" not in body

    def test_the_source_row_lists_each_agent_with_its_count(self, client: Client) -> None:
        self._backlog()

        response = client.get("/security/?agent=xbow")

        options = {option["key"]: option for option in response.context["scope_options"]}
        assert options["xbow"]["count"] == 2
        assert options["claude-code"]["count"] == 1
        assert options["none"]["count"] == 1
        assert options["all"]["count"] == 4
        # How the agent's reports were ruled, on hover.
        assert "1 ruled valid" in options["xbow"]["breakdown"]
        assert 'aria-current="true"' in response.content.decode()

    def test_every_link_that_keeps_the_view_carries_the_scope(self, client: Client) -> None:
        self._backlog()

        body = client.get("/security/?agent=xbow&status=new").content.decode()

        # Export, tabs and sort links: none of them may drop the scope on a click.
        assert "/security/export.csv?sort=priority&amp;status=new&amp;agent=xbow" in body
        assert "?sort=priority&amp;status=valid&amp;agent=xbow" in body
        assert "?sort=newest&amp;status=new&amp;agent=xbow" in body

    def test_every_bulk_form_carries_the_scope(self, client: Client) -> None:
        self._backlog()

        body = client.get("/security/?agent=xbow").content.decode()

        # The eleven existing bulk buttons, Confirm / Re-verify, and the two forms
        # on each archive row (none here) all post it.
        assert body.count('<input type="hidden" name="agent" value="xbow">') == 12
        assert "Acting on: reports from agent xbow." in body

    def test_no_source_row_until_some_report_has_an_agent(self, client: Client) -> None:
        SecurityReportFactory(source_agent="")

        body = client.get("/security/").content.decode()

        assert 'aria-label="Filter by source"' not in body

    def test_the_row_says_which_agent_wrote_it(self, client: Client) -> None:
        self._backlog()

        body = client.get("/security/").content.decode()

        assert "🤖 xbow" in body

    def test_a_scope_that_does_not_parse_shows_everything_with_a_warning(
        self, client: Client
    ) -> None:
        """This page only reads, so falling back is safe here — the buttons refuse."""
        self._backlog()

        response = client.get("/security/?agent=%26%26%26", follow=True)

        assert response.context["agent_scope"] == "all"
        assert any("isn't an agent name" in message for message in _messages(response))

    def test_an_agent_with_no_reports_shows_its_chip_and_an_honest_empty_list(
        self, client: Client
    ) -> None:
        self._backlog()

        response = client.get("/security/?agent=nobody")

        body = response.content.decode()
        assert "No reports on this tab among reports from agent nobody." in body
        assert {option["key"] for option in response.context["scope_options"]} >= {"nobody"}


@pytest.mark.django_db
class TestExportScope:
    def test_the_export_holds_only_the_scopes_reports(self, client: Client) -> None:
        SecurityReportFactory(title="Scanner finding", source_agent="xbow")
        SecurityReportFactory(title="Person-sent", source_agent="")

        response = client.get("/security/export.csv?agent=xbow")

        text = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
        assert "Scanner finding" in text
        assert "Person-sent" not in text
        assert "-agent-xbow-" in response["Content-Disposition"]

    def test_a_scope_that_does_not_parse_is_refused_not_widened(self, client: Client) -> None:
        response = client.get("/security/export.csv?agent=%26%26%26")

        assert response.status_code == 400


@pytest.mark.django_db
class TestImportLabels:
    def test_the_paste_form_stores_the_normalised_agent(self, client: Client) -> None:
        client.post(
            "/security/new/", {"raw_text": REPORT_TEXT, "source_agent": "Claude Code"}, follow=True
        )

        assert SecurityReport.objects.get().source_agent == "claude-code"

    def test_the_paste_form_refuses_a_reserved_name(self, client: Client) -> None:
        response = client.post("/security/new/", {"raw_text": REPORT_TEXT, "source_agent": "any"})

        assert response.status_code == 400
        assert not SecurityReport.objects.exists()

    def test_the_paste_form_offers_known_agents(self, client: Client) -> None:
        SecurityReportFactory(source_agent="xbow")

        body = client.get("/security/new/").content.decode()

        assert '<option value="xbow">' in body

    def _zip(self) -> SimpleUploadedFile:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("one.txt", REPORT_TEXT)
        return SimpleUploadedFile("scan.zip", buf.getvalue(), content_type="application/zip")

    def test_the_upload_labels_every_report(self, client: Client) -> None:
        response = client.post(
            "/security/upload/", {"zip_file": self._zip(), "source_agent": "xbow"}, follow=True
        )

        assert response.status_code == 200
        assert SecurityReport.objects.get().source_agent == "xbow"

    def test_the_upload_refuses_a_reserved_name_and_imports_nothing(self, client: Client) -> None:
        response = client.post(
            "/security/upload/", {"zip_file": self._zip(), "source_agent": "none"}, follow=True
        )

        assert not SecurityReport.objects.exists()
        assert any("reserved" in message for message in _messages(response))


@pytest.mark.django_db
class TestArchiveRelabel:
    def test_every_report_from_the_archive_gets_the_label(self, client: Client) -> None:
        one = SecurityReportFactory(source_archive="scan.zip")
        two = SecurityReportFactory(source_archive="scan.zip")
        other = SecurityReportFactory(source_archive="other.zip")

        response = client.post(
            "/security/archive-agent/",
            {"archive": "scan.zip", "source_agent": "XBOW"},
            follow=True,
        )

        for report in (one, two, other):
            report.refresh_from_db()
        assert (one.source_agent, two.source_agent, other.source_agent) == ("xbow", "xbow", "")
        assert any("Labelled 2 report(s)" in message for message in _messages(response))

    def test_blank_clears_the_label(self, client: Client) -> None:
        report = SecurityReportFactory(source_archive="scan.zip", source_agent="xbow")

        response = client.post(
            "/security/archive-agent/", {"archive": "scan.zip", "source_agent": ""}, follow=True
        )

        report.refresh_from_db()
        assert report.source_agent == ""
        assert any("Cleared" in message for message in _messages(response))

    def test_a_reserved_name_relabels_nothing(self, client: Client) -> None:
        report = SecurityReportFactory(source_archive="scan.zip", source_agent="xbow")

        response = client.post(
            "/security/archive-agent/", {"archive": "scan.zip", "source_agent": "all"}, follow=True
        )

        report.refresh_from_db()
        assert report.source_agent == "xbow"
        assert any("reserved" in message for message in _messages(response))

    def test_no_archive_and_a_gone_archive_say_so(self, client: Client) -> None:
        missing = client.post("/security/archive-agent/", {"source_agent": "x"}, follow=True)
        gone = client.post(
            "/security/archive-agent/", {"archive": "gone.zip", "source_agent": "x"}, follow=True
        )

        assert any("No archive named" in message for message in _messages(missing))
        assert any("No reports left" in message for message in _messages(gone))

    def test_the_archive_table_shows_the_label_and_a_mixed_archive(self, client: Client) -> None:
        SecurityReportFactory(source_archive="scan.zip", source_agent="xbow")
        SecurityReportFactory(source_archive="scan.zip", source_agent="")
        SecurityReportFactory(source_archive="clean.zip", source_agent="xbow")

        response = client.get("/security/")

        archives = {row["source_archive"]: row for row in response.context["archives"]}
        assert archives["scan.zip"]["agent_mixed"] is True
        assert archives["clean.zip"]["agent_mixed"] is False
        assert archives["clean.zip"]["agent"] == "xbow"


@pytest.mark.django_db
class TestScopedBulkButtons:
    def test_a_scoped_press_touches_only_that_agents_reports(self, client: Client) -> None:
        agents = SecurityReportFactory(raw_text=AUTH_OFF_TEXT, source_agent="xbow")
        persons = SecurityReportFactory(raw_text=AUTH_OFF_TEXT, source_agent="")

        response = _post(client, "/security/rerun-procedural/", None, agent="xbow")

        agents.refresh_from_db()
        persons.refresh_from_db()
        assert agents.auto_triage_status == "invalid"
        assert persons.auto_triage_status == ""
        assert any("(reports from agent xbow)" in message for message in _messages(response))

    def test_the_redirect_lands_back_on_the_same_source(self, client: Client) -> None:
        response = client.post("/security/rerun-procedural/", {"agent": "xbow"})

        assert response["Location"] == "/security/?agent=xbow"

    def test_a_scope_that_does_not_parse_runs_nothing(self, client: Client) -> None:
        """Never widened to all: the buttons spend LLM calls and agent runs."""
        report = SecurityReportFactory(raw_text=AUTH_OFF_TEXT)

        response = client.post("/security/rerun-procedural/", {"agent": "&&&"}, follow=True)

        report.refresh_from_db()
        assert report.auto_triage_status == ""
        assert any("isn't an agent scope" in message for message in _messages(response))

    @pytest.mark.parametrize(
        "path",
        [
            "/security/rerun-triage/",
            "/security/rerun-triage-failed/",
            "/security/rerun-duplicates/",
            "/security/recheck-fixed/",
            "/security/check-valid-fixed/",
            "/security/fix-landed-recheck/",
            "/security/match-branches/",
            "/security/scan-fixed/",
            "/security/fixes-landed/",
            "/security/cve-fixes-landed/",
            "/security/reverify/",
        ],
    )
    def test_every_bulk_button_refuses_an_unreadable_scope(self, client: Client, path: str) -> None:
        SecurityReportFactory()

        response = client.post(path, {"agent": "&&&"}, follow=True)

        assert any("isn't an agent scope" in message for message in _messages(response))
        assert not WorkerCommand.objects.exists()

    def test_rerun_triage_queues_only_the_scope(self, client: Client) -> None:
        agents = SecurityReportFactory(source_agent="xbow")
        SecurityReportFactory(source_agent="")

        _post(client, "/security/rerun-triage/", _triage_operator(), agent="xbow")

        queued = WorkerCommand.objects.filter(command="run_security_triage")
        assert list(queued.values_list("security_report_id", flat=True)) == [agents.pk]

    def test_rerun_failed_triage_queues_only_the_scope(self, client: Client) -> None:
        agents = SecurityReportFactory(source_agent="xbow")
        persons = SecurityReportFactory(source_agent="")
        for report in (agents, persons):
            WorkerCommand.objects.create(
                command="run_security_triage", security_report=report, status="failed"
            )

        _post(client, "/security/rerun-triage-failed/", _triage_operator(), agent="xbow")

        pending = WorkerCommand.objects.filter(command="run_security_triage", status="pending")
        assert list(pending.values_list("security_report_id", flat=True)) == [agents.pk]

    def test_the_duplicate_recheck_compares_only_the_scope(self, client: Client) -> None:
        agents = SecurityReportFactory(source_agent="xbow")
        SecurityReportFactory(source_agent="")

        with patch(
            "franktheunicorn.security.duplicates.redetect_across_backlog", return_value=(0, 0)
        ) as redetect:
            _post(client, "/security/rerun-duplicates/", _triage_operator(), agent="xbow")

        assert redetect.call_args.args[0] == [agents]

    @pytest.mark.parametrize(
        ("path", "command"),
        [
            ("/security/match-branches/", "match_security_branches"),
            ("/security/fixes-landed/", "check_fixes_landed"),
            ("/security/cve-fixes-landed/", "check_cve_fixes_landed"),
            ("/security/scan-fixed/", "scan_security_fixed"),
        ],
    )
    def test_the_git_sweeps_queue_with_the_scope(
        self, client: Client, path: str, command: str
    ) -> None:
        SecurityReportFactory(
            source_agent="xbow",
            matched_cve_id="CVE-2026-11111",
            fix_branch_sha="abc123",
            proposed_patch="--- a/x\n+++ b/x\n",
        )

        _post(client, path, _verifier_operator(), agent="xbow")

        assert WorkerCommand.objects.get(command=command).kwargs == {"agent_scope": "xbow"}

    def test_a_git_sweep_with_nothing_in_scope_queues_nothing(self, client: Client) -> None:
        SecurityReportFactory(source_agent="")

        response = _post(client, "/security/match-branches/", _verifier_operator(), agent="xbow")

        assert not WorkerCommand.objects.exists()
        assert any("No open report" in message for message in _messages(response))

    def test_a_running_sweep_over_everything_covers_a_scoped_press(self, client: Client) -> None:
        SecurityReportFactory(source_agent="xbow")
        WorkerCommand.objects.create(command="match_security_branches", status="running")

        response = _post(client, "/security/match-branches/", _verifier_operator(), agent="xbow")

        assert WorkerCommand.objects.filter(command="match_security_branches").count() == 1
        assert any("already queued or running" in message for message in _messages(response))

    def test_the_untriaged_recheck_launches_only_for_the_scope(self, client: Client) -> None:
        agents = SecurityReportFactory(status="new", source_agent="xbow")
        SecurityReportFactory(status="new", source_agent="", project=ProjectFactory())

        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.security.recheck.create_cursor_agent",
                return_value=("bc-x", "run-x"),
            ),
        ):
            _post(client, "/security/recheck-fixed/", make_operator_config(), agent="xbow")

        run = SecurityRecheckRun.objects.get()
        assert run.project == agents.project
        assert run.report_count == 1

    def test_the_valid_check_and_fix_landed_fallback_pass_the_scope(self, client: Client) -> None:
        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "key"}),
            patch("franktheunicorn.security.recheck.valid_reports", return_value=[]) as valid,
            patch(
                "franktheunicorn.security.recheck.fix_landed_candidates", return_value={}
            ) as landed,
        ):
            valid_response = _post(
                client, "/security/check-valid-fixed/", make_operator_config(), agent="none"
            )
            _post(client, "/security/fix-landed-recheck/", make_operator_config(), agent="none")

        valid.assert_called_once_with("none")
        landed.assert_called_once_with("none")
        assert any(
            "(reports not from an agent)" in message for message in _messages(valid_response)
        )


@pytest.mark.django_db
class TestConfirmReverify:
    """The bulk Confirm / Re-verify button: is each issue actually in the code?"""

    def _commands(self, command: str) -> list[int]:
        return sorted(
            WorkerCommand.objects.filter(command=command).values_list(
                "security_report_id", flat=True
            )
        )

    def test_git_depth_maps_every_open_report_in_scope_and_sends_none_to_the_agent(
        self, client: Client
    ) -> None:
        one = SecurityReportFactory(source_agent="xbow")
        two = SecurityReportFactory(source_agent="xbow", status="valid")
        SecurityReportFactory(source_agent="xbow", status="invalid")  # ruled out
        SecurityReportFactory(source_agent="")  # out of scope

        response = _post(
            client, "/security/reverify/", _verifier_operator(), agent="xbow", depth="git"
        )

        assert self._commands("map_report_versions") == sorted([one.pk, two.pk])
        assert self._commands("verify_security_report") == []
        assert any("2 queued for the git check" in message for message in _messages(response))

    def test_agent_depth_sends_a_capped_batch_and_says_how_many_are_left(
        self, client: Client
    ) -> None:
        reports = [SecurityReportFactory(source_agent="xbow") for _ in range(3)]

        with patch("franktheunicorn.security.verifier.MAX_BULK_VERIFY", 2):
            response = _post(
                client, "/security/reverify/", _verifier_operator(), agent="xbow", depth="agent"
            )

        assert len(self._commands("map_report_versions")) == 3
        assert len(self._commands("verify_security_report")) == 2
        messages = _messages(response)
        assert any("2 sent to the coding agent" in message for message in messages)
        assert any("1 more not sent" in message for message in messages)
        del reports

    def test_the_next_press_carries_on_rather_than_repeating(self, client: Client) -> None:
        reports = [SecurityReportFactory(source_agent="xbow") for _ in range(3)]

        with patch("franktheunicorn.security.verifier.MAX_BULK_VERIFY", 2):
            _post(client, "/security/reverify/", _verifier_operator(), depth="agent")
            WorkerCommand.objects.update(status="completed")
            _post(client, "/security/reverify/", _verifier_operator(), depth="agent")

        sent = WorkerCommand.objects.filter(command="verify_security_report")
        # The second press reached the third report before re-sending the first two.
        assert set(sent.values_list("security_report_id", flat=True)) == {r.pk for r in reports}

    def test_a_double_press_is_counted_not_doubled(self, client: Client) -> None:
        SecurityReportFactory()

        _post(client, "/security/reverify/", _verifier_operator(), depth="agent")
        response = _post(client, "/security/reverify/", _verifier_operator(), depth="agent")

        assert WorkerCommand.objects.filter(command="verify_security_report").count() == 1
        messages = _messages(response)
        assert any("1 already had one in flight" in message for message in messages)
        assert any("1 already being verified" in message for message in messages)

    def test_the_verifier_gate_is_said_on_the_page(self, client: Client) -> None:
        from franktheunicorn.config.models import (
            OperatorConfig,
            SecurityTriageConfig,
            SecurityVerifierConfig,
        )

        SecurityReportFactory()
        operator = OperatorConfig(
            security_triage=SecurityTriageConfig(verifier=SecurityVerifierConfig(enabled=False))
        )

        response = _post(client, "/security/reverify/", operator, depth="git")

        assert not WorkerCommand.objects.exists()
        assert any("Can't verify against the code" in message for message in _messages(response))

    def test_an_unknown_depth_queues_nothing(self, client: Client) -> None:
        SecurityReportFactory()

        response = _post(client, "/security/reverify/", _verifier_operator(), depth="deep")

        assert not WorkerCommand.objects.exists()
        assert any("Unknown verification depth" in message for message in _messages(response))

    def test_nothing_to_verify_says_why(self, client: Client) -> None:
        SecurityReportFactory(status="invalid")

        response = _post(client, "/security/reverify/", _verifier_operator(), depth="git")

        assert not WorkerCommand.objects.exists()
        assert any("Nothing to verify" in message for message in _messages(response))
