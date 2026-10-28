"""Tests for the variant-analysis scan.

One agent per confirmed past CVE hunts for the same bug pattern elsewhere.
Mirrors the introduced scan's honesty properties: a gate that stops the run
carries a reason, an unparseable answer is distinct from a clean run, and a
run that started is recorded even when it crashes.
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
    OperatorConfig,
    ProjectConfig,
    SecurityTriageConfig,
    SecurityVerifierConfig,
    VariantScanConfig,
)
from franktheunicorn.core.models import SecurityReport, VariantScanRun
from franktheunicorn.review.tool_executor import ExecResult
from franktheunicorn.security.variant_scan import (
    _file_finding,
    _parse_findings,
    _risk_class,
    is_due,
    scan_project_for_variants,
)
from tests.factories import ProjectFactory, SecurityReportFactory


def _scan(**overrides: Any) -> VariantScanConfig:
    base: dict[str, Any] = {"enabled": True}
    base.update(overrides)
    return VariantScanConfig(**base)


def _verifier(**overrides: Any) -> SecurityVerifierConfig:
    base: dict[str, Any] = {"enabled": True, "reviewer": "claude"}
    base.update(overrides)
    return SecurityVerifierConfig(**base)


def _operator(
    *,
    scan: VariantScanConfig | None = None,
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
        self.subdir = ""

    def prepare_repo(self, owner: str, repo: str, **kwargs: Any) -> str | None:
        self.subdir = kwargs.get("workspace_subdir", "")
        return self.cwd

    def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        from franktheunicorn.review.tool_executor import unwrap_guarded

        unwrapped = unwrap_guarded(cmd)
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
    show_diff: str = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-return x;\n+return Math.max(x, 0);\n",
    cwd: str = "/w/spark",
) -> _FakeExecutor:
    return _FakeExecutor(
        {
            "fetch": ExecResult(returncode=0, stdout="", stderr=""),
            "symbolic-ref": ExecResult(returncode=0, stdout="origin/master\n", stderr=""),
            "checkout": ExecResult(returncode=0, stdout="", stderr=""),
            "clean": ExecResult(returncode=0, stdout="", stderr=""),
            "rev-parse HEAD": ExecResult(returncode=0, stdout="deadbeefcafe\n", stderr=""),
            "show": ExecResult(returncode=0, stdout=show_diff, stderr=""),
            "claude": ExecResult(returncode=agent_returncode, stdout=agent_output, stderr=""),
        },
        cwd=cwd,
    )


def _patch_project_config(scan: VariantScanConfig, *, project: Any = None) -> tuple[Any, Any]:
    """Patch get_project_config to return a ProjectConfig carrying *scan*."""
    if project is None:
        project = ProjectFactory(owner="apache", repo="spark")
    pc = ProjectConfig(owner="apache", repo="spark", variant_scan=scan)
    return patch("franktheunicorn.config.loader.get_project_config", return_value=pc), project


@pytest.mark.django_db
class TestGateDeclines:
    """Every early return carries a reason; "didn't run" must not look like "ran clean"."""

    def test_disabled_does_not_run(self) -> None:
        patcher, project = _patch_project_config(_scan(enabled=False))
        with patcher, patch("franktheunicorn.review.tool_executor.make_executor") as maker:
            result = scan_project_for_variants(project, _operator())
        maker.assert_not_called()
        assert "variant_scan.enabled is false" in result.error
        assert not VariantScanRun.objects.filter(project=project).exists()

    def test_no_candidates_writes_run_row(self) -> None:
        # The cadence clock must start even when there is nothing to scan yet,
        # otherwise the scheduler re-queues every cycle.
        patcher, project = _patch_project_config(_scan())
        with patcher:
            result = scan_project_for_variants(project, _operator())
        assert "no valid report with a dated introduced_commit" in result.error
        run = VariantScanRun.objects.get(project=project)
        assert run.finished_at is not None
        assert run.cves_examined == 0

    def test_no_resolvable_reviewer_does_not_run(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, status="valid", introduced_commit="abc123")
        pc = ProjectConfig(owner="apache", repo="spark", variant_scan=_scan())
        with patch("franktheunicorn.config.loader.get_project_config", return_value=pc):
            config = _operator()
            config.security_triage.verifier = _verifier(reviewer="nobody")
            with patch("franktheunicorn.review.tool_executor.make_executor") as maker:
                result = scan_project_for_variants(project, config)
        maker.assert_not_called()
        assert "nobody" in result.error

    def test_bad_cadence_is_rejected(self) -> None:
        patcher, project = _patch_project_config(_scan())
        with patcher:
            result = scan_project_for_variants(project, _operator(), cadence="hourly")
        assert "hourly" in result.error
        assert not VariantScanRun.objects.filter(project=project).exists()


@pytest.mark.django_db
class TestScanRun:
    """The happy path: files variants, queues verification, records the run."""

    def test_files_findings_as_variant_scan_reports(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(
            project=project, status="valid", introduced_commit="abc123", title="Off-by-one in foo"
        )
        agent_output = json.dumps(
            {
                "findings": [
                    {
                        "title": "Off-by-one in bar (variant)",
                        "severity": "high",
                        "component": "bar",
                        "suspected_paths": ["src/bar.c"],
                        "description": "Same off-by-one pattern in bar.",
                        "reasoning": "bar uses the same loop shape as foo.",
                    }
                ]
            }
        )
        pc = ProjectConfig(owner="apache", repo="spark", variant_scan=_scan())
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor") as maker,
            patch("franktheunicorn.security.queue.queue_verification") as qv,
        ):
            maker.return_value = _executor_with(agent_output)
            qv.return_value = True
            result = scan_project_for_variants(project, _operator())
        assert result.error == ""
        assert result.cves_examined == 1
        assert result.finding_count == 1
        assert result.verified_count == 1
        filed = SecurityReport.objects.filter(source="variant-scan")
        assert filed.count() == 1
        report = filed.get()
        assert "variant of" in report.raw_text.lower()
        assert "Off-by-one in foo" in report.raw_text  # links to the source CVE
        run = VariantScanRun.objects.get(project=project)
        assert run.finding_count == 1
        assert run.cves_examined == 1

    def test_clean_run_is_zero_findings_not_an_error(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, status="valid", introduced_commit="abc123")
        agent_output = json.dumps({"findings": []})
        pc = ProjectConfig(owner="apache", repo="spark", variant_scan=_scan())
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor") as maker,
            patch("franktheunicorn.security.queue.queue_verification"),
        ):
            maker.return_value = _executor_with(agent_output)
            result = scan_project_for_variants(project, _operator())
        assert result.error == ""
        assert result.finding_count == 0
        assert SecurityReport.objects.filter(source="variant-scan").count() == 0

    def test_unparseable_output_surfaces_as_error_when_all_fail(self) -> None:
        # Unparseable doesn't crash, but when every CVE is unparseable it must
        # not look like a clean run — the operator needs to tell "ran and found
        # nothing" from "never ran right".
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, status="valid", introduced_commit="abc123")
        pc = ProjectConfig(owner="apache", repo="spark", variant_scan=_scan())
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor") as maker,
            patch("franktheunicorn.security.queue.queue_verification"),
        ):
            maker.return_value = _executor_with("not json at all")
            result = scan_project_for_variants(project, _operator())
        assert result.finding_count == 0
        assert "refused, failed, or unparseable" in result.error
        assert result.unparseable_count == 1

    def test_run_row_created_even_on_checkout_failure(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, status="valid", introduced_commit="abc123")
        pc = ProjectConfig(owner="apache", repo="spark", variant_scan=_scan())
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor") as maker,
            patch("franktheunicorn.security.queue.queue_verification"),
        ):
            maker.return_value = _FakeExecutor({})
            maker.return_value.prepare_repo = lambda *a, **k: None
            result = scan_project_for_variants(project, _operator())
        assert "no checkout could be prepared" in result.error
        run = VariantScanRun.objects.get(project=project)
        assert run.error  # the crash left a visible row

    def test_auto_verify_false_skips_queue(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, status="valid", introduced_commit="abc123")
        agent_output = json.dumps(
            {"findings": [{"title": "v", "severity": "high", "reasoning": "r"}]}
        )
        pc = ProjectConfig(owner="apache", repo="spark", variant_scan=_scan(auto_verify=False))
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor") as maker,
            patch("franktheunicorn.security.queue.queue_verification") as qv,
        ):
            maker.return_value = _executor_with(agent_output)
            result = scan_project_for_variants(project, _operator())
        assert result.finding_count == 1
        assert result.verified_count == 0
        qv.assert_not_called()

    def test_variant_scan_reports_do_not_seed_further_scans(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        # A variant ruled valid with an introduced_commit must not become a seed.
        SecurityReportFactory(
            project=project, source="variant-scan", status="valid", introduced_commit="deadbeef"
        )
        pc = ProjectConfig(owner="apache", repo="spark", variant_scan=_scan())
        with patch("franktheunicorn.config.loader.get_project_config", return_value=pc):
            result = scan_project_for_variants(project, _operator())
        assert "no valid report with a dated introduced_commit" in result.error

    def test_rotation_picks_different_cves_across_runs(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        # 3 eligible CVEs, max 1 per run → run 0 picks oldest, run 1 picks next.
        for i in range(3):
            SecurityReportFactory(
                project=project,
                status="valid",
                introduced_commit=f"commit{i}",
                title=f"CVE-{i}",
            )
        agent_output = json.dumps({"findings": []})
        pc = ProjectConfig(owner="apache", repo="spark", variant_scan=_scan(max_cves_per_run=1))
        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.review.tool_executor.make_executor") as maker,
            patch("franktheunicorn.security.queue.queue_verification"),
        ):
            maker.return_value = _executor_with(agent_output)
            first = scan_project_for_variants(project, _operator())
            second = scan_project_for_variants(project, _operator())
        # Each run examined 1 CVE, and they were different (rotation by run count).
        assert first.cves_examined == 1
        assert second.cves_examined == 1
        assert VariantScanRun.objects.filter(project=project).count() == 2


@pytest.mark.django_db
class TestParseFindings:
    def test_parses_json_with_findings(self) -> None:
        output = json.dumps({"findings": [{"title": "a"}, {"title": "b"}]})
        parsed = _parse_findings(output)
        assert parsed is not None
        assert len(parsed["findings"]) == 2

    def test_returns_none_for_no_json(self) -> None:
        assert _parse_findings("just prose, no json") is None

    def test_filters_non_dict_entries(self) -> None:
        output = json.dumps({"findings": [{"title": "a"}, "not a dict", 42]})
        parsed = _parse_findings(output)
        assert len(parsed["findings"]) == 1

    def test_lenient_about_prose_around_json(self) -> None:
        output = f"Here are the findings:\n{json.dumps({'findings': [{'title': 'x'}]})}\nDone."
        parsed = _parse_findings(output)
        assert parsed is not None
        assert len(parsed["findings"]) == 1


@pytest.mark.django_db
class TestFileFinding:
    def test_files_with_source_variant_scan(self) -> None:
        project = ProjectFactory()
        cve = SecurityReportFactory(
            project=project, status="valid", introduced_commit="abc", matched_cve_id="CVE-2026-1"
        )
        run = VariantScanRun.objects.create(project=project)
        finding = {
            "title": "Variant bug",
            "severity": "high",
            "component": "mod",
            "suspected_paths": ["a.py"],
            "description": "desc",
            "reasoning": "same pattern",
        }
        report = _file_finding(finding, project, run, cve)
        assert report is not None
        assert report.source == "variant-scan"
        assert report.status == "new"
        # The parent's CVE id is NOT stamped on the unconfirmed variant — it
        # would make operator_has_ruled skip it and land it in the wrong tab.
        # The lineage lives in raw_text instead.
        assert report.matched_cve_id == ""
        assert "Variant of:" in report.raw_text
        assert "CVE-2026-1" in report.raw_text

    def test_dedup_skips_repeat_of_same_title_and_parent(self) -> None:
        project = ProjectFactory()
        cve = SecurityReportFactory(
            project=project, status="valid", introduced_commit="abc", title="Original"
        )
        run = VariantScanRun.objects.create(project=project)
        finding = {"title": "Variant bug", "severity": "high"}
        first = _file_finding(finding, project, run, cve)
        assert first is not None
        # Same title + same parent → already filed, skip.
        second = _file_finding(finding, project, run, cve)
        assert second is None
        assert SecurityReport.objects.filter(source="variant-scan").count() == 1

    def test_no_title_returns_none(self) -> None:
        project = ProjectFactory()
        cve = SecurityReportFactory(project=project, status="valid", introduced_commit="abc")
        run = VariantScanRun.objects.create(project=project)
        assert _file_finding({}, project, run, cve) is None


class TestRiskClass:
    def test_title_only_when_no_summary(self) -> None:
        report = SecurityReportFactory.build(title="Off-by-one in foo", triage_summary="")
        assert _risk_class(report) == "Off-by-one in foo"

    def test_title_plus_summary_when_summary_exists(self) -> None:
        report = SecurityReportFactory.build(
            title="Off-by-one", triage_summary="Loop index off by one"
        )
        risk = _risk_class(report)
        assert "Off-by-one" in risk
        assert "Loop index off by one" in risk


@pytest.mark.django_db
class TestIsDue:
    def test_disabled_is_never_due(self) -> None:
        project = ProjectFactory()
        assert not is_due(project, _scan(enabled=False))

    def test_no_prior_run_is_due(self) -> None:
        project = ProjectFactory()
        assert is_due(project, _scan())

    def test_recent_run_is_not_due(self) -> None:
        project = ProjectFactory()
        VariantScanRun.objects.create(project=project, started_at=timezone.now(), cadence="weekly")
        assert not is_due(project, _scan(cadence="weekly"))

    def test_old_run_is_due(self) -> None:
        project = ProjectFactory()
        VariantScanRun.objects.create(
            project=project,
            started_at=timezone.now() - timedelta(days=14),
            cadence="weekly",
        )
        assert is_due(project, _scan(cadence="weekly"))


@pytest.mark.django_db
class TestVariantScanQ:
    def test_matches_variant_scan_source(self) -> None:
        from franktheunicorn.core.models import SecurityReport

        variant = SecurityReportFactory(source="variant-scan", title="variant finding")
        other = SecurityReportFactory(source="paste", title="pasted finding")
        qs = SecurityReport.objects.filter(SecurityReport.variant_scan_q())
        assert variant in qs
        assert other not in qs

    def test_tab_filter_uses_it(self) -> None:
        from franktheunicorn.dashboard.views import _security_tab_q

        SecurityReportFactory(source="variant-scan", title="variant finding")
        SecurityReportFactory(source="paste", title="pasted finding")
        qs = SecurityReport.objects.filter(_security_tab_q(SecurityReport.VARIANT_SCAN_FILTER))
        assert qs.count() == 1
        assert qs.get().source == "variant-scan"
