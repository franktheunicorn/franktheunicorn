"""Tests for the scheduled introduced-CVE scan.

The scan walks a project's recent changes for newly introduced CVEs, given
the project's prior accepted/rejected CVE reports as context, files each
finding as a SecurityReport with source="introduced-scan", and queues exploit
verification on each. Honesty properties mirror the siblings: a gate that
stops the run carries a reason, an unparseable answer is distinguishable from
a clean "no findings" run, and a run that started is recorded even when it
crashes.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from django.utils import timezone

from franktheunicorn.config.models import (
    AgentCLIReviewerConfig,
    IntroducedScanConfig,
    OperatorConfig,
    SecurityTriageConfig,
    SecurityVerifierConfig,
)
from franktheunicorn.core.models import IntroducedScanRun, SecurityReport
from franktheunicorn.review.tool_executor import ExecResult
from franktheunicorn.security.introduced_scan import (
    _file_finding,
    _parse_findings,
    _prior_context,
    is_due,
    scan_project_for_introduced_cves,
)
from tests.factories import ProjectFactory, SecurityReportFactory


def _scan(**overrides: Any) -> IntroducedScanConfig:
    base: dict[str, Any] = {"enabled": True}
    base.update(overrides)
    return IntroducedScanConfig(**base)


def _verifier(**overrides: Any) -> SecurityVerifierConfig:
    base: dict[str, Any] = {"enabled": True, "reviewer": "claude"}
    base.update(overrides)
    return SecurityVerifierConfig(**base)


def _operator(
    *,
    scan: IntroducedScanConfig | None = None,
    verifier: SecurityVerifierConfig | None = None,
) -> OperatorConfig:
    config = OperatorConfig()
    config.security_triage = SecurityTriageConfig()
    config.security_triage.verifier = verifier or _verifier()
    config.agent_cli_reviewers = [AgentCLIReviewerConfig(name="claude", cli_path="claude")]
    return config


class _FakeExecutor:
    """Scripted executor: maps an argv prefix to a result."""

    def __init__(self, responses: dict[str, Any], cwd: str = "/w/spark") -> None:
        self.responses = responses
        self.cwd = cwd
        self.calls: list[list[str]] = []
        self.guarded: list[bool] = []
        self.subdir = ""

    def prepare_repo(self, owner: str, repo: str, **kwargs: Any) -> str | None:
        self.subdir = kwargs.get("workspace_subdir", "")
        return self.cwd

    def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        from franktheunicorn.review.tool_executor import unwrap_guarded

        unwrapped = unwrap_guarded(cmd)
        self.guarded.append(unwrapped is not cmd)
        cmd = unwrapped
        self.calls.append(cmd)
        shape = " ".join(cmd[:4]) if cmd and cmd[0] == "git" else (cmd[0] if cmd else "")
        for key, response in self.responses.items():
            if key in shape:
                return response
        return ExecResult(returncode=0, stdout="", stderr="")


def _executor_with(
    agent_output: str,
    *,
    agent_returncode: int = 0,
    daily_commits: str = "aaa111\nbbb222\n",
    tags: str = "v3.5.0\nv3.5.0-RC1\nv3.4.0\n",
    diff: str = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-return x;\n+return Math.max(x, 0);\n",
    cwd: str = "/w/spark",
) -> _FakeExecutor:
    return _FakeExecutor(
        {
            "fetch": ExecResult(returncode=0, stdout="", stderr=""),
            "symbolic-ref": ExecResult(returncode=0, stdout="origin/master\n", stderr=""),
            "checkout": ExecResult(returncode=0, stdout="", stderr=""),
            "clean": ExecResult(returncode=0, stdout="", stderr=""),
            "rev-parse HEAD": ExecResult(returncode=0, stdout="deadbeefcafe\n", stderr=""),
            "log --since": ExecResult(returncode=0, stdout=daily_commits, stderr=""),
            "tag --sort": ExecResult(returncode=0, stdout=tags, stderr=""),
            "diff": ExecResult(returncode=0, stdout=diff, stderr=""),
            "claude": ExecResult(returncode=agent_returncode, stdout=agent_output, stderr=""),
        },
        cwd=cwd,
    )


def _patch_project_config(scan: IntroducedScanConfig) -> Any:
    """Patch get_project_config to return a ProjectConfig carrying *scan*.

    The scan reads the project's introduced_scan via get_project_config, so a
    test that wants a particular config has to patch the loader rather than the
    model — the model is per-project config, not operator config.
    """
    from franktheunicorn.config.models import ProjectConfig

    project = ProjectFactory(owner="apache", repo="spark")
    pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=scan)
    return patch("franktheunicorn.config.loader.get_project_config", return_value=pc), project


@pytest.mark.django_db
class TestGateDeclines:
    """Every early return carries a reason; "didn't run" must not look like "ran clean"."""

    def test_disabled_does_not_run(self) -> None:
        patcher, project = _patch_project_config(_scan(enabled=False))
        with patcher, patch("franktheunicorn.review.tool_executor.make_executor") as maker:
            result = scan_project_for_introduced_cves(project, _operator())
        maker.assert_not_called()
        assert "introduced_scan.enabled is false" in result.error
        assert not IntroducedScanRun.objects.filter(project=project).exists()

    def test_no_resolvable_reviewer_does_not_run(self) -> None:
        patcher, project = _patch_project_config(_scan())
        config = _operator()
        config.security_triage.verifier = _verifier(reviewer="nobody")
        with patcher, patch("franktheunicorn.review.tool_executor.make_executor") as maker:
            result = scan_project_for_introduced_cves(project, config)
        maker.assert_not_called()
        assert "nobody" in result.error
        assert not IntroducedScanRun.objects.filter(project=project).exists()

    def test_bad_cadence_is_rejected(self) -> None:
        patcher, project = _patch_project_config(_scan())
        with patcher:
            result = scan_project_for_introduced_cves(project, _operator(), cadence="hourly")
        assert "hourly" in result.error
        assert not IntroducedScanRun.objects.filter(project=project).exists()


@pytest.mark.django_db
class TestIsDue:
    """The scheduler's throttle: a project is due when no run exists or the cadence elapsed."""

    def test_no_prior_run_is_due(self) -> None:
        project = ProjectFactory()
        assert is_due(project, _scan()) is True

    def test_disabled_is_never_due(self) -> None:
        project = ProjectFactory()
        assert is_due(project, _scan(enabled=False)) is False

    def test_a_run_today_is_not_due_for_daily(self) -> None:
        project = ProjectFactory()
        IntroducedScanRun.objects.create(
            project=project, started_at=timezone.now(), cadence="daily"
        )
        assert is_due(project, _scan()) is False

    def test_a_run_two_days_ago_is_due_for_daily(self) -> None:
        project = ProjectFactory()
        IntroducedScanRun.objects.create(
            project=project,
            started_at=timezone.now() - timedelta(days=2, hours=1),
            cadence="daily",
        )
        assert is_due(project, _scan()) is True

    def test_a_run_three_days_ago_is_not_due_for_weekly(self) -> None:
        project = ProjectFactory()
        IntroducedScanRun.objects.create(
            project=project,
            started_at=timezone.now() - timedelta(days=3),
            cadence="weekly",
        )
        assert is_due(project, _scan(cadence="weekly")) is False

    def test_a_run_eight_days_ago_is_due_for_weekly(self) -> None:
        project = ProjectFactory()
        IntroducedScanRun.objects.create(
            project=project,
            started_at=timezone.now() - timedelta(days=8),
            cadence="weekly",
        )
        assert is_due(project, _scan(cadence="weekly")) is True

    def test_a_crashed_run_still_counts_as_the_last_run(self) -> None:
        """A run that started but never finished is the last run — no retry storm."""
        project = ProjectFactory()
        IntroducedScanRun.objects.create(
            project=project,
            started_at=timezone.now() - timedelta(minutes=5),
            cadence="daily",
            finished_at=None,
        )
        assert is_due(project, _scan()) is False

    def test_an_explicit_now_is_honoured(self) -> None:
        """The scheduler passes now= for testability; the gate uses it over timezone.now()."""
        project = ProjectFactory()
        IntroducedScanRun.objects.create(
            project=project,
            started_at=timezone.now() - timedelta(days=3),
            finished_at=timezone.now() - timedelta(days=3, minutes=1),
            cadence="daily",
        )
        # 3 days after a daily run, with an explicit now a day further out: still due.
        assert (
            is_due(project, _scan(cadence="daily"), now=timezone.now() + timedelta(days=1)) is True
        )


class TestParseFindings:
    def test_happy_list(self) -> None:
        out = json.dumps({"findings": [{"title": "X", "severity": "high"}]})
        assert _parse_findings(out) == [{"title": "X", "severity": "high"}]

    def test_empty_list_is_a_clean_run(self) -> None:
        assert _parse_findings(json.dumps({"findings": []})) == []

    def test_no_json_is_none(self) -> None:
        assert _parse_findings("I had a look, nothing to report.") is None

    def test_missing_findings_key_is_none(self) -> None:
        assert _parse_findings(json.dumps({"summary": "clean"})) is None

    def test_non_dict_entries_are_dropped(self) -> None:
        out = json.dumps({"findings": [{"title": "X"}, "not a dict", None]})
        assert _parse_findings(out) == [{"title": "X"}]

    def test_json_wrapped_in_prose_is_taken(self) -> None:
        wrapped = 'Here:\n{"findings": [{"title": "Y"}]}\nDone.'
        assert _parse_findings(wrapped) == [{"title": "Y"}]


@pytest.mark.django_db
class TestFileFinding:
    def test_creates_a_report_with_the_introduced_scan_source(self) -> None:
        project = ProjectFactory()
        run_row = IntroducedScanRun.objects.create(
            project=project, cadence="daily", commit_range="last 2 days"
        )
        finding = {
            "title": "Negative foo bypasses validation",
            "severity": "high",
            "component": "SQL",
            "suspected_paths": ["sql/foo.py", "sql/bar.py"],
            "description": "Negative foo is not rejected and reaches the planner.",
            "reasoning": "The hunk removed the Math.max guard.",
        }
        report = _file_finding(finding, project, run_row)
        assert report is not None
        report.refresh_from_db()
        assert report.source == "introduced-scan"
        assert report.status == "new"
        assert report.title == "Negative foo bypasses validation"
        assert report.claimed_severity == "high"
        assert report.parsed_component == "SQL"
        assert "sql/foo.py" in report.raw_text
        assert "Math.max guard" in report.raw_text
        assert "cadence daily" in report.raw_text

    def test_a_finding_with_no_title_is_dropped(self) -> None:
        project = ProjectFactory()
        run_row = IntroducedScanRun.objects.create(project=project, cadence="daily")
        assert _file_finding({"title": "", "severity": "high"}, project, run_row) is None
        assert _file_finding({"severity": "high"}, project, run_row) is None
        assert not SecurityReport.objects.filter(source="introduced-scan").exists()


@pytest.mark.django_db
class TestPriorContext:
    def test_accepted_and_rejected_reports_are_separated(self) -> None:
        project = ProjectFactory()
        SecurityReportFactory(
            project=project, status="valid", title="RCE via X", matched_cve_id="CVE-2026-1"
        )
        SecurityReportFactory(project=project, status="invalid", title="Not a hole")
        SecurityReportFactory(project=project, status="duplicate", title="Dup of CVE-1")
        SecurityReportFactory(project=project, status="new", title="Untriaged")
        accepted, rejected = _prior_context(project)
        assert "RCE via X" in accepted
        assert "CVE-2026-1" in accepted
        assert "Not a hole" in rejected
        assert "Dup of CVE-1" in rejected
        # A new/untriaged report is neither accepted nor rejected context.
        assert "Untriaged" not in accepted
        assert "Untriaged" not in rejected

    def test_no_prior_reports_is_none_markers(self) -> None:
        project = ProjectFactory()
        accepted, rejected = _prior_context(project)
        assert accepted == "(none)"
        assert rejected == "(none)"


@pytest.mark.django_db
class TestAgentRun:
    """The happy path: findings are filed and exploit verification is queued."""

    def _run_scan(
        self,
        agent_output: str,
        *,
        scan: IntroducedScanConfig | None = None,
        agent_returncode: int = 0,
        daily_commits: str = "aaa111\n",
        tags: str = "v3.5.0\n",
        diff: str = "--- a/f\n+++ b/f\n",
    ) -> tuple[Any, _FakeExecutor, Any, Any]:
        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        scan = scan or _scan()
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=scan)
        config = _operator()
        executor = _executor_with(
            agent_output,
            agent_returncode=agent_returncode,
            daily_commits=daily_commits,
            tags=tags,
            diff=diff,
        )
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch(
                "franktheunicorn.security.queue.queue_exploit_verification", return_value=True
            ) as q,
        ):
            result = scan_project_for_introduced_cves(project, config)
        return result, executor, project, q

    def test_daily_run_files_findings_and_queues_verify(self) -> None:
        agent_json = json.dumps(
            {
                "findings": [
                    {
                        "title": "Negative foo bypasses validation",
                        "severity": "high",
                        "component": "SQL",
                        "suspected_paths": ["sql/foo.py"],
                        "description": "Negative foo reaches the planner.",
                        "reasoning": "The hunk removed the guard.",
                    }
                ]
            }
        )
        result, _executor, project, q = self._run_scan(agent_json)

        assert result.error == ""
        assert result.cadence == "daily"
        assert "last 2 day(s)" in result.commit_range
        assert result.branch == "master"
        assert result.finding_count == 1
        assert result.verified_count == 1
        report = SecurityReport.objects.get(source="introduced-scan")
        assert report.title == "Negative foo bypasses validation"
        q.assert_called_once_with(report)
        # A run row was recorded, finished, and counts the finding.
        run_row = IntroducedScanRun.objects.get(project=project)
        assert run_row.finished_at is not None
        assert run_row.finding_count == 1
        assert run_row.verified_count == 1
        assert run_row.error == ""

    def test_no_findings_is_a_clean_run_not_an_error(self) -> None:
        result, _executor, _project, _q = self._run_scan(json.dumps({"findings": []}))
        assert result.error == ""
        assert result.finding_count == 0
        assert result.verified_count == 0
        assert not SecurityReport.objects.filter(source="introduced-scan").exists()

    def test_no_json_is_logged_not_treated_as_findings(self) -> None:
        result, _executor, _project, _q = self._run_scan("I looked, nothing to report.")
        assert result.error == ""
        assert result.finding_count == 0
        assert result.raw_output  # the unparseable answer is kept

    def test_agent_failure_is_an_error_with_a_run_row(self) -> None:
        result, _executor, project, _q = self._run_scan("oops", agent_returncode=1)
        assert "exited 1" in result.error
        # The run row is still recorded — it started, then the agent failed.
        run_row = IntroducedScanRun.objects.get(project=project)
        assert run_row.error
        assert run_row.finding_count == 0

    def test_auto_verify_off_does_not_queue_verify(self) -> None:
        result, _executor, _project, q = self._run_scan(
            json.dumps({"findings": [{"title": "X", "severity": "low"}]}),
            scan=_scan(auto_verify=False),
        )
        assert result.finding_count == 1
        assert result.verified_count == 0
        q.assert_not_called()

    def test_max_findings_caps_the_filed_count(self) -> None:
        findings = [{"title": f"Finding {i}", "severity": "low"} for i in range(5)]
        result, _executor, _project, _q = self._run_scan(
            json.dumps({"findings": findings}), scan=_scan(max_findings=2)
        )
        assert result.finding_count == 2
        assert SecurityReport.objects.filter(source="introduced-scan").count() == 2

    def test_weekly_uses_the_last_release_tag(self) -> None:
        result, executor, _project, _q = self._run_scan(
            json.dumps({"findings": []}),
            scan=_scan(cadence="weekly"),
            tags="v3.5.0\nv3.5.0-RC1\nv3.4.0\n",
        )
        # weekly/monthly diffs from the last final release tag; the RC is skipped.
        assert "v3.5.0..HEAD" in result.commit_range
        diff_call = next(c for c in executor.calls if c[:2] == ["git", "diff"])
        assert "v3.5.0..HEAD" in diff_call

    def test_weekly_with_no_release_tag_is_a_clean_run(self) -> None:
        result, _executor, _project, _q = self._run_scan(
            json.dumps({"findings": []}),
            scan=_scan(cadence="weekly"),
            tags="v3.5.0-RC1\n",  # only an RC, no final release
        )
        assert result.error == ""
        assert "none found" in result.commit_range
        assert result.finding_count == 0

    def test_daily_with_no_commits_is_a_clean_run(self) -> None:
        result, _executor, _project, _q = self._run_scan(
            json.dumps({"findings": []}), daily_commits=""
        )
        assert result.error == ""
        assert result.finding_count == 0

    def test_every_agent_run_is_guarded_and_no_git_call_is(self) -> None:
        _result, executor, _project, _q = self._run_scan(json.dumps({"findings": []}))
        runs = list(zip(executor.calls, executor.guarded, strict=True))
        agent_runs = [g for cmd, g in runs if cmd[0] != "git"]
        git_runs = [g for cmd, g in runs if cmd[0] == "git"]
        assert agent_runs and all(agent_runs)
        assert git_runs and not any(git_runs)

    def test_the_diff_reaches_the_agent_prompt(self) -> None:
        _result, executor, _project, _q = self._run_scan(
            json.dumps({"findings": []}),
            diff="@@ -1 +1 @@\n-return x;\n+return Math.max(x, 0);\n",
        )
        prompt = next(c for c in executor.calls if c[0] == "claude")[-1]
        assert "Math.max(x, 0)" in prompt
        assert "master" in prompt

    def test_the_scan_model_falls_back_to_the_verifier_model(self) -> None:
        _result, executor, _project, _q = self._run_scan(
            json.dumps({"findings": []}),
            scan=_scan(model=""),
        )
        # _operator() leaves verifier.model empty too, so the reviewer's own
        # model is used. Assert the agent was invoked (model fallback path ran).
        assert any(c[0] == "claude" for c in executor.calls)

    def test_extra_args_reach_the_agent(self) -> None:
        _result, executor, _project, _q = self._run_scan(
            json.dumps({"findings": []}), scan=_scan(extra_args=["--effort", "max"])
        )
        agent_call = next(c for c in executor.calls if c[0] == "claude")
        assert "--effort" in agent_call and "max" in agent_call

    def test_workspace_subdir_is_passed_through(self) -> None:
        _result, executor, _project, _q = self._run_scan(
            json.dumps({"findings": []}), scan=_scan(workspace_subdir="security-introduced")
        )
        assert executor.subdir == "security-introduced"

    def test_summary_line_names_counts(self) -> None:
        result, _executor, _project, _q = self._run_scan(
            json.dumps({"findings": [{"title": "X", "severity": "low"}]})
        )
        assert "1 finding(s) filed" in result.summary()
        assert "1 queued for verify" in result.summary()


@pytest.mark.django_db
class TestEdgeBranches:
    """The early returns and git/agent failures that are a clean run or a logged
    error, never a crash. Each carries a reason so "didn't run" isn't "ran clean"."""

    def _run(
        self,
        *,
        patches: list[Any],
        agent_output: str = "",
        agent_returncode: int = 0,
        daily_commits: str = "aaa111\n",
        tags: str = "v3.5.0\n",
        diff: str = "--- a/f\n+++ b/f\n",
        cwd: str = "/w/spark",
    ) -> tuple[Any, Any]:
        import contextlib

        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        scan = _scan()
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=scan)
        config = _operator()
        executor = _executor_with(
            agent_output,
            agent_returncode=agent_returncode,
            daily_commits=daily_commits,
            tags=tags,
            diff=diff,
            cwd=cwd,
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch("franktheunicorn.config.loader.get_project_config", return_value=pc)
            )
            stack.enter_context(
                patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor)
            )
            stack.enter_context(
                patch(
                    "franktheunicorn.security.queue.queue_exploit_verification",
                    return_value=True,
                )
            )
            for p in patches:
                stack.enter_context(p)
            result = scan_project_for_introduced_cves(project, config)
        return result, project

    def test_no_project_config_is_a_logged_skip(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        with patch("franktheunicorn.config.loader.get_project_config", return_value=None):
            result = scan_project_for_introduced_cves(project, _operator())
        assert "no project config" in result.error
        assert not IntroducedScanRun.objects.filter(project=project).exists()

    def test_no_checkout_prepared_is_an_error_with_a_run_row(self) -> None:
        result, project = self._run(
            patches=[],
            cwd="",  # prepare_repo returns "" -> no checkout
            agent_output=json.dumps({"findings": []}),
        )
        assert "no checkout could be prepared" in result.error
        assert IntroducedScanRun.objects.filter(project=project).exists()

    def test_no_default_branch_is_an_error(self) -> None:
        result, project = self._run(
            patches=[
                patch("franktheunicorn.security.introduced_scan._default_branch", return_value="")
            ],
            agent_output=json.dumps({"findings": []}),
        )
        assert "no default branch" in result.error
        assert IntroducedScanRun.objects.filter(project=project).exists()

    def test_checkout_fails_is_an_error(self) -> None:
        # _checkout (the commit step) returns "" -> "could not check out".
        result, project = self._run(
            patches=[patch("franktheunicorn.security.introduced_scan._checkout", return_value="")],
            agent_output=json.dumps({"findings": []}),
        )
        assert "could not check out" in result.error
        assert IntroducedScanRun.objects.filter(project=project).exists()

    def test_a_stale_refresh_is_warned_not_fatal(self) -> None:
        result, _project = self._run(
            patches=[
                patch(
                    "franktheunicorn.security.introduced_scan.refresh_from_upstream",
                    return_value="fetch refused",
                )
            ],
            agent_output=json.dumps({"findings": []}),
        )
        # A stale refresh is a warning, not a stop — the scan still completes.
        assert result.error == ""
        assert result.commit_range.startswith("last 2 day")

    def test_a_failed_daily_diff_is_a_clean_run(self) -> None:
        # A diff that exits non-zero is "nothing to review", not an error.
        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=_scan())
        executor = _FakeExecutor(
            {
                "fetch": ExecResult(returncode=0, stdout="", stderr=""),
                "symbolic-ref": ExecResult(returncode=0, stdout="origin/master\n", stderr=""),
                "checkout": ExecResult(returncode=0, stdout="", stderr=""),
                "clean": ExecResult(returncode=0, stdout="", stderr=""),
                "rev-parse HEAD": ExecResult(returncode=0, stdout="deadbeefcafe\n", stderr=""),
                "log --since": ExecResult(returncode=0, stdout="aaa111\n", stderr=""),
                "tag --sort": ExecResult(returncode=0, stdout="v3.5.0\n", stderr=""),
                "diff": ExecResult(returncode=1, stdout="", stderr="boom"),
                "claude": ExecResult(returncode=0, stdout="{}", stderr=""),
            }
        )
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch("franktheunicorn.security.queue.queue_exploit_verification", return_value=True),
        ):
            result = scan_project_for_introduced_cves(project, _operator())
        assert result.error == ""
        assert result.commit_range.startswith("last 2 day")

    def test_a_failed_since_release_diff_is_a_clean_run(self) -> None:
        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=_scan(cadence="weekly"))
        executor = _FakeExecutor(
            {
                "fetch": ExecResult(returncode=0, stdout="", stderr=""),
                "symbolic-ref": ExecResult(returncode=0, stdout="origin/master\n", stderr=""),
                "checkout": ExecResult(returncode=0, stdout="", stderr=""),
                "clean": ExecResult(returncode=0, stdout="", stderr=""),
                "rev-parse HEAD": ExecResult(returncode=0, stdout="deadbeefcafe\n", stderr=""),
                "log --since": ExecResult(returncode=0, stdout="aaa111\n", stderr=""),
                "tag --sort": ExecResult(returncode=0, stdout="v3.5.0\n", stderr=""),
                "diff": ExecResult(returncode=1, stdout="", stderr="boom"),
                "claude": ExecResult(returncode=0, stdout="{}", stderr=""),
            }
        )
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch("franktheunicorn.security.queue.queue_exploit_verification", return_value=True),
        ):
            result = scan_project_for_introduced_cves(project, _operator(), cadence="weekly")
        assert result.error == ""
        assert result.commit_range == "v3.5.0..HEAD"

    def test_a_failed_tag_list_is_no_release(self) -> None:
        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=_scan(cadence="weekly"))
        executor = _FakeExecutor(
            {
                "fetch": ExecResult(returncode=0, stdout="", stderr=""),
                "symbolic-ref": ExecResult(returncode=0, stdout="origin/master\n", stderr=""),
                "checkout": ExecResult(returncode=0, stdout="", stderr=""),
                "clean": ExecResult(returncode=0, stdout="", stderr=""),
                "rev-parse HEAD": ExecResult(returncode=0, stdout="deadbeefcafe\n", stderr=""),
                "log --since": ExecResult(returncode=0, stdout="aaa111\n", stderr=""),
                "tag --sort": ExecResult(returncode=1, stdout="", stderr="boom"),
                "diff": ExecResult(returncode=0, stdout="diff\n", stderr=""),
                "claude": ExecResult(returncode=0, stdout="{}", stderr=""),
            }
        )
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch("franktheunicorn.security.queue.queue_exploit_verification", return_value=True),
        ):
            result = scan_project_for_introduced_cves(project, _operator(), cadence="weekly")
        assert result.error == ""
        assert result.commit_range == "since last release (none found)"

    def test_agent_timeout_is_an_error(self) -> None:
        # The claude call returns None (timeout); git calls still succeed.
        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=_scan())
        executor = _FakeExecutor(
            {
                "fetch": ExecResult(returncode=0, stdout="", stderr=""),
                "symbolic-ref": ExecResult(returncode=0, stdout="origin/master\n", stderr=""),
                "checkout": ExecResult(returncode=0, stdout="", stderr=""),
                "clean": ExecResult(returncode=0, stdout="", stderr=""),
                "rev-parse HEAD": ExecResult(returncode=0, stdout="deadbeefcafe\n", stderr=""),
                "log --since": ExecResult(returncode=0, stdout="aaa111\n", stderr=""),
                "tag --sort": ExecResult(returncode=0, stdout="v3.5.0\n", stderr=""),
                "diff": ExecResult(returncode=0, stdout="diff\n", stderr=""),
                "claude": None,  # timeout — no result
            }
        )
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch("franktheunicorn.security.queue.queue_exploit_verification", return_value=True),
        ):
            result = scan_project_for_introduced_cves(project, _operator())
        assert "no result within" in result.error
        assert IntroducedScanRun.objects.filter(project=project).exists()

    def test_guard_refusal_is_an_error(self) -> None:
        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=_scan())
        executor = _executor_with("{}", agent_returncode=1)
        # guard_refusal returns a non-empty string -> the CLI "did not run".
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch("franktheunicorn.review.tool_executor.guard_refusal", return_value="refused"),
            patch("franktheunicorn.security.queue.queue_exploit_verification", return_value=True),
        ):
            result = scan_project_for_introduced_cves(project, _operator())
        assert "did not run: refused" in result.error

    def test_auth_failure_advice_is_appended(self) -> None:
        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=_scan())
        executor = _executor_with("oops", agent_returncode=1)
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch(
                "franktheunicorn.review.tool_executor.looks_like_auth_failure",
                return_value=True,
            ),
            patch(
                "franktheunicorn.review.tool_executor.looks_like_workspace_trust_refusal",
                return_value=False,
            ),
            patch("franktheunicorn.review.tool_executor.agent_stderr", return_value="auth err"),
            patch(
                "franktheunicorn.review.tool_executor.auth_failure_advice", return_value="ADVICE"
            ),
            patch("franktheunicorn.security.queue.queue_exploit_verification", return_value=True),
        ):
            result = scan_project_for_introduced_cves(project, _operator())
        assert "exited 1" in result.error
        assert "ADVICE" in result.error

    def test_workspace_trust_advice_is_appended(self) -> None:
        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=_scan())
        executor = _executor_with("oops", agent_returncode=1)
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch(
                "franktheunicorn.review.tool_executor.looks_like_auth_failure",
                return_value=False,
            ),
            patch(
                "franktheunicorn.review.tool_executor.looks_like_workspace_trust_refusal",
                return_value=True,
            ),
            patch("franktheunicorn.review.tool_executor.agent_stderr", return_value="trust err"),
            patch(
                "franktheunicorn.review.tool_executor.workspace_trust_advice",
                return_value="TRUST-ADVICE",
            ),
            patch("franktheunicorn.security.queue.queue_exploit_verification", return_value=True),
        ):
            result = scan_project_for_introduced_cves(project, _operator())
        assert "exited 1" in result.error
        assert "TRUST-ADVICE" in result.error

    def test_prompt_addendum_reaches_the_agent(self) -> None:
        from franktheunicorn.config.models import ProjectConfig

        project = ProjectFactory(owner="apache", repo="spark")
        scan = _scan(prompt_addendum="Look hard at deserialization.")
        pc = ProjectConfig(owner="apache", repo="spark", introduced_scan=scan)
        executor = _executor_with(json.dumps({"findings": []}))
        captured: dict[str, Any] = {}

        def _capture_prompt(*_args: Any, **kwargs: Any) -> str:
            captured.update(kwargs)
            return "prompt"

        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch(
                "franktheunicorn.security.introduced_scan._build_prompt",
                side_effect=_capture_prompt,
            ),
            patch("franktheunicorn.security.queue.queue_exploit_verification", return_value=True),
        ):
            scan_project_for_introduced_cves(project, _operator())
        assert captured.get("addendum") == "Look hard at deserialization."


def test_parse_findings_skips_malformed_json() -> None:
    """A blob that isn't valid JSON is skipped, not fatal — the next candidate wins."""
    out = 'Here you go:\n```json\n{not valid}\n```\n```json\n{"findings": []}\n```'
    assert _parse_findings(out) == []
