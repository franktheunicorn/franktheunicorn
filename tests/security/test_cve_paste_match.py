"""Tests for the pasted-CVE-to-report matcher (security.cve_paste_match).

The matcher takes CVE ids the operator pasted (assigned but not yet public)
and has an agent tie each to a SecurityReport already in the backlog. No live
agent runs — ``make_executor`` is patched to a fake that returns scripted JSON.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import (
    AgentCLIReviewerConfig,
    LLMBackendConfig,
    OperatorConfig,
    SecurityCveMatchConfig,
    SecurityTriageConfig,
)
from franktheunicorn.core.models import TentativeCveMatch, WorkerCommand
from franktheunicorn.review.tool_executor import ExecResult, unwrap_guarded
from franktheunicorn.security.cve_paste_match import (
    PastedCve,
    match_one_cve,
    match_pending_for_project,
    parse_pasted_cves,
    resolve_match_reviewer,
    upsert_matches,
)
from tests.factories import ProjectFactory, SecurityReportFactory

pytestmark = pytest.mark.django_db


def _match_config(**overrides: Any) -> SecurityCveMatchConfig:
    base: dict[str, Any] = {"reviewer": "claude"}
    base.update(overrides)
    return SecurityCveMatchConfig(**base)


def _operator(match: SecurityCveMatchConfig | None = None) -> OperatorConfig:
    config = OperatorConfig(
        github_username="holdenk", llm_backends=[LLMBackendConfig(provider="stub")]
    )
    config.security_triage = SecurityTriageConfig(enabled=True)
    config.security_triage.cve_match = match or _match_config()
    config.agent_cli_reviewers = [AgentCLIReviewerConfig(name="claude", cli_path="claude")]
    return config


class _FakeExecutor:
    """Scripted executor: maps argv[0] to a result."""

    def __init__(self, response: ExecResult | None, cwd: str = "/tmp/frank-cve-match") -> None:
        self.response = response
        self.cwd = cwd
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        unwrapped = unwrap_guarded(cmd)
        self.calls.append(unwrapped)
        return self.response


# --- parse_pasted_cves -------------------------------------------------------


class TestParsePastedCves:
    def test_one_cve_per_line_with_description(self) -> None:
        out = parse_pasted_cves("CVE-2026-12345: RCE in the foo parser\nCVE-2026-12346: DoS in bar")
        assert [p.cve_id for p in out] == ["CVE-2026-12345", "CVE-2026-12346"]
        assert out[0].description == "RCE in the foo parser"
        assert out[1].description == "DoS in bar"

    def test_continuation_lines_extend_the_previous_description(self) -> None:
        out = parse_pasted_cves(
            "CVE-2026-12345: RCE in foo\nvia crafted input\nCVE-2026-00002: second"
        )
        assert len(out) == 2
        assert "via crafted input" in out[0].description

    def test_lines_without_a_cve_id_are_ignored_when_no_prior_entry(self) -> None:
        out = parse_pasted_cves("just some text\nno cve here")
        assert out == []

    def test_bare_case_is_normalised(self) -> None:
        out = parse_pasted_cves("cve-2026-12345: lowercased")
        assert out[0].cve_id == "CVE-2026-12345"

    def test_empty_paste_returns_empty(self) -> None:
        assert parse_pasted_cves("") == []


# --- upsert_matches ----------------------------------------------------------


class TestUpsertMatches:
    def test_creates_pending_rows(self) -> None:
        project = ProjectFactory()
        rows = upsert_matches(project, [PastedCve(cve_id="CVE-2026-1", description="d")])
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].cve_description == "d"

    def test_re_paste_updates_description_and_requeues_no_match(self) -> None:
        project = ProjectFactory()
        first = upsert_matches(project, [PastedCve(cve_id="CVE-2026-1", description="old")])[0]
        first.status = "no_match"
        first.save()

        second = upsert_matches(project, [PastedCve(cve_id="CVE-2026-1", description="new")])[0]
        assert second.pk == first.pk  # same row, not a duplicate
        assert second.status == "pending"  # re-queued
        assert second.cve_description == "new"

    def test_re_paste_leaves_a_confirmed_row_alone(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project)
        first = upsert_matches(project, [PastedCve(cve_id="CVE-2026-1", description="old")])[0]
        first.status = "confirmed"
        first.report = report
        first.save()

        second = upsert_matches(project, [PastedCve(cve_id="CVE-2026-1", description="new")])[0]
        assert second.status == "confirmed"  # the operator already ruled
        assert second.report_id == report.pk
        # description still refreshes — the operator pasted an update.
        assert second.cve_description == "new"

    def test_unique_per_project_cve_id(self) -> None:
        project = ProjectFactory()
        upsert_matches(project, [PastedCve(cve_id="CVE-2026-1")])
        upsert_matches(project, [PastedCve(cve_id="CVE-2026-1")])
        assert TentativeCveMatch.objects.filter(project=project).count() == 1

    def test_same_cve_in_two_projects_is_two_rows(self) -> None:
        a, b = ProjectFactory(), ProjectFactory()
        upsert_matches(a, [PastedCve(cve_id="CVE-2026-1")])
        upsert_matches(b, [PastedCve(cve_id="CVE-2026-1")])
        assert TentativeCveMatch.objects.count() == 2


# --- resolve_match_reviewer --------------------------------------------------


class TestResolveMatchReviewer:
    def test_returns_the_named_entry(self) -> None:
        config = _operator()
        assert resolve_match_reviewer(config, config.security_triage.cve_match).name == "claude"

    def test_returns_none_when_no_match_and_logs(self) -> None:
        config = _operator()
        config.security_triage.cve_match = _match_config(reviewer="ghost")
        assert resolve_match_reviewer(config, config.security_triage.cve_match) is None


# --- match_one_cve / match_pending_for_project --------------------------------


def _agent_response(report_id: int | None, confidence: str, reasoning: str) -> ExecResult:
    return ExecResult(
        returncode=0,
        stdout=json.dumps(
            {"report_id": report_id, "confidence": confidence, "reasoning": reasoning}
        ),
        stderr="",
    )


class TestMatchOneCve:
    def _setup(self, response: ExecResult | None) -> tuple[TentativeCveMatch, _FakeExecutor, Any]:
        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(project=project, title="RCE in foo parser", raw_text="body")
        row = upsert_matches(project, [PastedCve(cve_id="CVE-2026-1", description="RCE")])[0]
        executor = _FakeExecutor(response)
        return row, executor, report

    def test_a_match_writes_tentative(self) -> None:
        row, executor, report = self._setup(
            _agent_response(report_id=1, confidence="high", reasoning="same hole")
        )
        result = match_one_cve(
            row, _operator().agent_cli_reviewers[0], _match_config(), executor, "/tmp"
        )
        assert result == "matched"
        row.refresh_from_db()
        assert row.status == "tentative"
        assert row.report_id == report.pk
        assert row.confidence == "high"

    def test_a_null_report_id_is_no_match(self) -> None:
        row, executor, _ = self._setup(
            _agent_response(report_id=None, confidence="none", reasoning="nothing fits")
        )
        result = match_one_cve(
            row, _operator().agent_cli_reviewers[0], _match_config(), executor, "/tmp"
        )
        assert result == "no_match"
        row.refresh_from_db()
        assert row.status == "no_match"
        assert row.report_id is None

    def test_a_hallucinated_report_id_is_no_match(self) -> None:
        # The agent names an id that isn't in the DB for this project.
        row, executor, _ = self._setup(
            _agent_response(report_id=99999, confidence="high", reasoning="x")
        )
        result = match_one_cve(
            row, _operator().agent_cli_reviewers[0], _match_config(), executor, "/tmp"
        )
        assert result == "no_match"
        row.refresh_from_db()
        assert "does not exist" in row.reasoning

    def test_an_unparseable_answer_is_no_match(self) -> None:
        row, executor, _ = self._setup(
            ExecResult(returncode=0, stdout="the agent rambled, no JSON", stderr="")
        )
        result = match_one_cve(
            row, _operator().agent_cli_reviewers[0], _match_config(), executor, "/tmp"
        )
        assert result == "no_match"
        row.refresh_from_db()
        assert row.reasoning == "The agent's answer could not be parsed."

    def test_a_guard_refusal_leaves_the_row_pending(self) -> None:
        # exit 97 + a frank: refusal note ending "; not running the agent" =
        # the guard declined to run the agent (see guard_refusal).
        row, executor, _ = self._setup(
            ExecResult(
                returncode=97,
                stdout="",
                stderr="frank: refused: no checkout config; not running the agent\n",
            )
        )
        result = match_one_cve(
            row, _operator().agent_cli_reviewers[0], _match_config(), executor, "/tmp"
        )
        assert result == "skipped"
        row.refresh_from_db()
        assert row.status == "pending"  # untouched — a re-run picks it up

    def test_a_nonzero_exit_is_error(self) -> None:
        row, executor, _ = self._setup(ExecResult(returncode=2, stdout="", stderr="boom"))
        result = match_one_cve(
            row, _operator().agent_cli_reviewers[0], _match_config(), executor, "/tmp"
        )
        assert result == "error"


class TestMatchPendingForProject:
    def test_no_reviewer_is_a_gate_decline_not_a_crash(self) -> None:
        project = ProjectFactory()
        upsert_matches(project, [PastedCve(cve_id="CVE-2026-1")])
        run = match_pending_for_project(project, _operator(match=_match_config(reviewer="ghost")))
        assert run.error.startswith("no agent_cli_reviewers")
        # The row stays pending.
        assert TentativeCveMatch.objects.get().status == "pending"

    def test_no_pending_rows_is_an_empty_run(self) -> None:
        project = ProjectFactory()
        run = match_pending_for_project(project, _operator())
        assert run.matched == 0 and run.no_match == 0
        assert run.notes  # "no pending CVEs to match"

    def test_it_loops_every_pending_row(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(project=project)
        upsert_matches(project, [PastedCve(cve_id="CVE-2026-1"), PastedCve(cve_id="CVE-2026-2")])
        executor = _FakeExecutor(
            _agent_response(report_id=report.pk, confidence="high", reasoning="x")
        )
        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            run = match_pending_for_project(project, _operator())
        assert run.matched == 2
        assert len(executor.calls) == 2

    def test_magpie_skills_block_reaches_the_prompt(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        upsert_matches(project, [PastedCve(cve_id="CVE-2026-1", description="d")])
        executor = _FakeExecutor(_agent_response(report_id=None, confidence="none", reasoning="x"))
        with (
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch(
                "franktheunicorn.security.cve_paste_match.skills_block_for_project",
                return_value="## Borrowed Magpie skill text\n\nMATCH-MARKER.",
            ) as sb,
        ):
            match_pending_for_project(project, _operator())
        sb.assert_called_once()
        prompt = executor.calls[0][-1]
        assert "MATCH-MARKER." in prompt


# --- worker dispatch ---------------------------------------------------------


class TestWorkerDispatch:
    def test_requires_project_id_in_kwargs(self) -> None:
        from franktheunicorn.worker.commands import _dispatch

        cmd = WorkerCommand.objects.create(command="match_cves_to_reports")
        with pytest.raises(ValueError, match="requires a project_id"):
            _dispatch(cmd, _operator())

    def test_a_missing_project_is_logged_not_crashed(self) -> None:
        from franktheunicorn.worker.commands import _dispatch

        cmd = WorkerCommand.objects.create(
            command="match_cves_to_reports", kwargs={"project_id": 99999}
        )
        _dispatch(cmd, _operator())
        assert "no longer exists" in cmd.log

    def test_it_runs_the_matcher_and_records_the_summary(self) -> None:
        from franktheunicorn.worker.commands import _dispatch

        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(project=project)
        upsert_matches(project, [PastedCve(cve_id="CVE-2026-1", description="d")])
        executor = _FakeExecutor(
            _agent_response(report_id=report.pk, confidence="high", reasoning="x")
        )
        cmd = WorkerCommand.objects.create(
            command="match_cves_to_reports", kwargs={"project_id": project.pk}
        )
        with (
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch(
                "franktheunicorn.security.cve_paste_match.skills_block_for_project",
                return_value="",
            ),
        ):
            _dispatch(cmd, _operator())
        assert "matched=1" in cmd.log
