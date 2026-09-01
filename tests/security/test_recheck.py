"""Tests for the batch "did recent commits fix these?" recheck.

Launch is one POST per project; the worker polls the run and writes per-report
verdicts. The HTTP is mocked throughout — these pin the prompt shape, the
verdict parsing, and what lands on the rows.
"""

from __future__ import annotations

import os
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from franktheunicorn.core.models import SecurityRecheckRun
from franktheunicorn.security.fix_agent import FixAgentError, RunGoneError
from franktheunicorn.security.recheck import (
    _verdicts_from,
    apply_recheck_results,
    build_recheck_prompt,
    launch_recheck,
    poll_rechecks,
    untriaged_by_project,
)
from tests.factories import (
    SecurityRecheckRunFactory,
    SecurityReportFactory,
    make_operator_config,
)


class TestUntriagedByProject:
    @pytest.mark.django_db
    def test_only_new_reports_with_a_project_are_covered(self) -> None:
        included = SecurityReportFactory(status="new")
        SecurityReportFactory(status="valid")  # ruled on
        SecurityReportFactory(status="new", project=None)  # no repo to check
        grouped = untriaged_by_project()
        assert list(grouped) == [included.project]
        assert grouped[included.project] == [included]

    @pytest.mark.django_db
    def test_a_report_with_a_fix_branch_is_not_batched(self) -> None:
        """This run asks whether recent commits fixed it; the operator has already
        answered by hand. Those rows otherwise consumed slots of the per-run cap and
        came back "still-valid", which the list renders beside the operator's branch."""
        included = SecurityReportFactory(status="new")
        SecurityReportFactory(status="new", project=included.project, fixed_in_branch="branch-3.5")

        grouped = untriaged_by_project()

        assert grouped[included.project] == [included]


class TestBuildRecheckPrompt:
    @pytest.mark.django_db
    def test_the_prompt_lists_reports_and_demands_json(self) -> None:
        report = SecurityReportFactory(
            title="mergeDir escapes", finding_id="f002", triage_summary="Path traversal."
        )
        prompt = build_recheck_prompt(report.project, [report], lookback_days=30)
        assert f"report #{report.pk}" in prompt
        assert "mergeDir escapes" in prompt
        assert "30 days" in prompt
        assert "likely-fixed" in prompt and "still-valid" in prompt
        assert "UNTRUSTED DATA" in prompt


class TestLaunchRecheck:
    @pytest.mark.django_db
    def test_a_launch_records_the_run_row(self) -> None:
        report = SecurityReportFactory(status="new")
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.security.recheck.create_cursor_agent",
                return_value=("bc-r", "run-r"),
            ) as mock_create,
        ):
            runs = launch_recheck(report.project, [report], make_operator_config())
        assert len(runs) == 1
        run = runs[0]
        assert run.agent_id == "bc-r"
        assert run.run_id == "run-r"
        assert run.status == "launched"
        assert run.report_count == 1
        assert mock_create.call_count == 1

    @pytest.mark.django_db
    def test_a_big_backlog_is_chunked_into_runs(self) -> None:
        # The agent owes one JSON object per report; past a few dozen the
        # answer truncates, which parses as zero verdicts.
        project = SecurityReportFactory(status="new").project
        reports = [SecurityReportFactory(status="new", project=project) for _ in range(51)]
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.security.recheck.create_cursor_agent",
                side_effect=[("bc-1", "run-1"), ("bc-2", "run-2")],
            ),
        ):
            runs = launch_recheck(project, reports, make_operator_config())
        assert len(runs) == 2
        assert sorted(run.report_count for run in runs) == [1, 50]

    @pytest.mark.django_db
    def test_no_api_key_raises_before_any_row(self) -> None:
        report = SecurityReportFactory(status="new")
        with (
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(FixAgentError, match="CURSOR_API_KEY"),
        ):
            launch_recheck(report.project, [report], make_operator_config())
        assert not SecurityRecheckRun.objects.exists()

    @pytest.mark.django_db
    def test_disabled_names_the_setting(self) -> None:
        report = SecurityReportFactory(status="new")
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            pytest.raises(FixAgentError, match=r"fix_agent\.enabled"),
        ):
            launch_recheck(report.project, [report], make_operator_config(enabled=False))
        assert not SecurityRecheckRun.objects.exists()


class TestVerdictsFrom:
    def test_a_bare_json_array_parses(self) -> None:
        rows = _verdicts_from('[{"report": 1, "verdict": "likely-fixed", "reason": "x"}]')
        assert rows == [{"report": 1, "verdict": "likely-fixed", "reason": "x"}]

    def test_a_fenced_array_parses(self) -> None:
        rows = _verdicts_from('```json\n[{"report": 2, "verdict": "still-valid"}]\n```')
        assert rows[0]["report"] == 2

    def test_prose_around_the_array_is_tolerated(self) -> None:
        rows = _verdicts_from('Here you go:\n[{"report": 3, "verdict": "still-valid"}]\nDone.')
        assert rows[0]["report"] == 3

    def test_no_array_is_empty_not_an_exception(self) -> None:
        assert _verdicts_from("I couldn't decide.") == []


class TestApplyRecheckResults:
    @pytest.mark.django_db
    def test_verdicts_land_on_the_reports(self) -> None:
        fixed = SecurityReportFactory(status="new")
        valid = SecurityReportFactory(status="new", project=fixed.project)
        run = SecurityRecheckRunFactory(project=fixed.project, report_count=2)
        result = (
            f'[{{"report": {fixed.pk}, "verdict": "likely-fixed", "reason": "abc123 rewrote it"}},'
            f' {{"report": {valid.pk}, "verdict": "still-valid", "reason": "nothing near it"}}]'
        )
        assert apply_recheck_results(run, result) == 2
        fixed.refresh_from_db()
        valid.refresh_from_db()
        assert fixed.recheck_status == "likely-fixed"
        assert fixed.recheck_reason == "abc123 rewrote it"
        assert fixed.rechecked_at is not None
        assert valid.recheck_status == "still-valid"

    @pytest.mark.django_db
    def test_a_ruled_report_is_not_touched(self) -> None:
        # The operator ruled between launch and finish; the machine's answer
        # must not write over a row that is no longer untriaged.
        report = SecurityReportFactory(status="valid")
        run = SecurityRecheckRunFactory(project=report.project)
        written = apply_recheck_results(
            run, f'[{{"report": {report.pk}, "verdict": "likely-fixed", "reason": "x"}}]'
        )
        assert written == 0
        report.refresh_from_db()
        assert report.recheck_status == ""

    @pytest.mark.django_db
    def test_another_projects_report_is_not_touched(self) -> None:
        # The prompt inlines bare pks; a hallucinated or stale one must not
        # write a verdict onto a report the run was never about.
        report = SecurityReportFactory(status="new")
        run = SecurityRecheckRunFactory()  # a different project
        written = apply_recheck_results(
            run, f'[{{"report": {report.pk}, "verdict": "likely-fixed", "reason": "x"}}]'
        )
        assert written == 0
        report.refresh_from_db()
        assert report.recheck_status == ""

    @pytest.mark.django_db
    def test_an_unknown_verdict_is_skipped(self) -> None:
        report = SecurityReportFactory(status="new")
        run = SecurityRecheckRunFactory(project=report.project)
        written = apply_recheck_results(
            run, f'[{{"report": {report.pk}, "verdict": "maybe", "reason": "x"}}]'
        )
        assert written == 0


class TestPollRechecks:
    """One pass, then hand the waiting back to the queue.

    This used to loop until every run was terminal or ``recheck_timeout_seconds``
    (default 3600) expired, in the worker lane reserved for work somebody is
    waiting on. Now it polls once and reports how many are still running so the
    caller can re-queue.
    """

    @pytest.mark.django_db
    def test_a_finished_run_writes_verdicts_and_marks_finished(self) -> None:
        report = SecurityReportFactory(status="new")
        run = SecurityRecheckRunFactory(project=report.project)
        data = {
            "status": "FINISHED",
            "result": f'[{{"report": {report.pk}, "verdict": "likely-fixed", "reason": "r"}}]',
        }
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch("franktheunicorn.security.recheck.fetch_run", return_value=data),
        ):
            assert poll_rechecks(make_operator_config()) == (1, 0, 0)
        run.refresh_from_db()
        assert run.status == "finished"
        report.refresh_from_db()
        assert report.recheck_status == "likely-fixed"

    @pytest.mark.django_db
    def test_a_failed_run_is_marked_error(self) -> None:
        SecurityRecheckRunFactory()
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.security.recheck.fetch_run",
                return_value={"status": "ERROR", "result": "boom"},
            ),
        ):
            assert poll_rechecks(make_operator_config()) == (0, 1, 0)

    @pytest.mark.django_db
    def test_a_transient_failure_leaves_the_run_for_the_next_pass(self) -> None:
        # None means the API hiccuped, not that the remote agent died — the run
        # stays launched and is reported as still running so the caller re-queues.
        run = SecurityRecheckRunFactory()
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch("franktheunicorn.security.recheck.fetch_run", return_value=None),
        ):
            assert poll_rechecks(make_operator_config()) == (0, 0, 1)
        run.refresh_from_db()
        assert run.status == "launched"

    @pytest.mark.django_db
    def test_a_gone_run_is_an_error_not_a_retry(self) -> None:
        # 404/410: nothing will ever finish this run.
        run = SecurityRecheckRunFactory()
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.security.recheck.fetch_run",
                side_effect=RunGoneError("Cursor API said 404: the run is gone"),
            ),
        ):
            assert poll_rechecks(make_operator_config()) == (0, 1, 0)
        run.refresh_from_db()
        assert run.status == "error"
        assert "gone" in run.detail

    @pytest.mark.django_db
    def test_an_old_run_times_out_but_a_young_one_survives(self) -> None:
        # Expiry is per run, from its own created_at: the run launched two hours
        # ago is stuck, the one launched a minute ago is still working.
        old = SecurityRecheckRunFactory(created_at=timezone.now() - timedelta(hours=2))
        young = SecurityRecheckRunFactory()
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.security.recheck.fetch_run",
                return_value={"status": "RUNNING"},
            ),
        ):
            assert poll_rechecks(make_operator_config(recheck_timeout_seconds=60)) == (0, 1, 1)
        old.refresh_from_db()
        young.refresh_from_db()
        assert old.status == "error"
        assert "gave up waiting" in old.detail
        assert young.status == "launched"

    @pytest.mark.django_db
    def test_a_finished_run_that_answered_nothing_is_not_a_success(self) -> None:
        # It cost a full agent run. "finished" would show it identically to one
        # that answered all fifty, and SecurityRecheckRun is on no page.
        run = SecurityRecheckRunFactory(report_count=50)
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.security.recheck.fetch_run",
                return_value={"status": "FINISHED", "result": "I could not check these."},
            ),
        ):
            assert poll_rechecks(make_operator_config()) == (0, 1, 0)
        run.refresh_from_db()
        assert run.status == "error"
        assert "no verdicts could be read" in run.detail

    @pytest.mark.django_db
    def test_a_keyless_worker_leaves_live_runs_alone(self) -> None:
        # The launch happens in the web process and the poll in the worker; under
        # compose they are separate containers. Marking these error threw away the
        # verdicts of agents that were still running and billing.
        run = SecurityRecheckRunFactory()
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": ""}),
            patch("franktheunicorn.security.recheck.fetch_run") as mock_fetch,
        ):
            assert poll_rechecks(make_operator_config()) == (0, 0, 1)
        assert not mock_fetch.called
        run.refresh_from_db()
        assert run.status == "launched"

    @pytest.mark.django_db
    def test_no_runs_is_a_clean_zero(self) -> None:
        with patch.dict(os.environ, {"CURSOR_API_KEY": "key"}):
            assert poll_rechecks(make_operator_config()) == (0, 0, 0)


class TestVerdictsSurviveRealisticAnswers:
    """Outermost-bracket slicing lost whole runs to a stray ``[``.

    The prompt asks for a bare array, so there is usually no code fence to lean
    on, and ``find("[")``/``rfind("]")`` then spanned whatever else the answer
    contained. The decode failed and came back as "no verdicts" — which looked
    exactly like an agent that answered nothing, on a run that still said
    "finished".
    """

    def test_a_citation_before_the_array(self) -> None:
        rows = _verdicts_from('See [1] for context.\n[{"report": 7, "verdict": "still-valid"}]')
        assert [row["report"] for row in rows] == [7]

    def test_a_checklist_line_before_the_array(self) -> None:
        rows = _verdicts_from('- [x] checked history\n[{"report": 8, "verdict": "likely-fixed"}]')
        assert [row["report"] for row in rows] == [8]

    def test_a_bracketed_aside_after_the_array(self) -> None:
        rows = _verdicts_from('[{"report": 9, "verdict": "still-valid"}]\n[end of report]')
        assert [row["report"] for row in rows] == [9]

    def test_the_longest_array_of_objects_wins(self) -> None:
        rows = _verdicts_from(
            '[1, 2]\n[{"report": 10, "verdict": "still-valid"}, '
            '{"report": 11, "verdict": "likely-fixed"}]'
        )
        assert [row["report"] for row in rows] == [10, 11]

    def test_a_truncated_array_is_still_nothing(self) -> None:
        assert _verdicts_from('[{"report": 12, "verdict": "still-') == []


class TestOneLiveRunPerProjectChunk:
    @pytest.mark.django_db
    def test_a_second_concurrent_launch_does_not_pay_twice(self) -> None:
        # The view read in-flight state and then created rows, so two overlapping
        # presses both saw nothing running and both launched a full paid run.
        report = SecurityReportFactory(status="new")
        SecurityRecheckRun.objects.create(
            project=report.project, status="launched", report_count=1, chunk_index=0
        )
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch("franktheunicorn.security.recheck.create_cursor_agent") as mock_create,
            pytest.raises(FixAgentError, match="already running"),
        ):
            launch_recheck(report.project, [report], make_operator_config())
        assert not mock_create.called

    @pytest.mark.django_db
    def test_a_finished_run_does_not_block_the_next_one(self) -> None:
        report = SecurityReportFactory(status="new")
        SecurityRecheckRun.objects.create(
            project=report.project, status="finished", report_count=1, chunk_index=0
        )
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.security.recheck.create_cursor_agent",
                return_value=("bc-1", "run-1"),
            ),
        ):
            runs = launch_recheck(report.project, [report], make_operator_config())
        assert len(runs) == 1

    @pytest.mark.django_db
    def test_a_failed_post_releases_the_slot(self) -> None:
        # Otherwise one unreachable-API press blocks the button for that project
        # until the stale sweep, an hour later.
        report = SecurityReportFactory(status="new")
        with (
            patch.dict(os.environ, {"CURSOR_API_KEY": "key"}),
            patch(
                "franktheunicorn.security.recheck.create_cursor_agent",
                side_effect=FixAgentError("could not reach the Cursor API"),
            ),
            pytest.raises(FixAgentError),
        ):
            launch_recheck(report.project, [report], make_operator_config())
        assert not SecurityRecheckRun.objects.filter(status="launched").exists()
