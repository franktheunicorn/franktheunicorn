"""Tests for the git-side fix-landed check.

The properties worth pinning are the two never-overwrite rules — an
indeterminate non-answer must not replace a verdict, and ``released`` is
terminal because tags do not un-happen — and the resolution order: the agent's
own sha beats the branch name, the branch name beats the operator's free text,
and a mainline branch name in that free text is a non-answer, not proof.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import AgentCLIReviewerConfig, OperatorConfig
from franktheunicorn.review.tool_executor import ExecResult
from franktheunicorn.security.fix_landed import (
    _find_pr,
    check_cve_fixes,
    check_fix_landed,
    check_project_fixes,
)
from tests.factories import ProjectFactory, SecurityReportFactory

_LISTING = ExecResult(
    returncode=0,
    stdout=(
        "origin/HEAD 1900000000\n"
        "origin/master 1900000000\n"
        "origin/fix-cve-2025-12345 1899000000\n"
        "origin/branch-3.5 1898000000\n"
    ),
    stderr="",
)


class _Executor:
    """Scripted git for the ancestry/tag/ref calls the check makes."""

    def __init__(
        self,
        *,
        shas: tuple[str, ...] = (),
        refs: dict[str, str] | None = None,
        ancestors: tuple[tuple[str, str], ...] = (),
        tags: dict[str, list[str]] | None = None,
        merge_base_returncode: int | None = None,
    ) -> None:
        self.shas = set(shas)
        self.refs = refs or {}
        self.ancestors = set(ancestors)
        self.tags = tags or {}
        #: When set, every merge-base answers with this code (128 = "no answer").
        self.merge_base_returncode = merge_base_returncode
        self.calls: list[list[str]] = []

    def prepare_repo(self, owner: str, repo: str, **kwargs: Any) -> str | None:
        return "/w/spark"

    def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        self.calls.append(cmd)
        if cmd[:2] == ["git", "cat-file"]:
            return ExecResult(returncode=0 if cmd[3] in self.shas else 128, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            sha = self.refs.get(cmd[-1], "")
            if sha:
                return ExecResult(returncode=0, stdout=sha + "\n", stderr="")
            return ExecResult(returncode=128, stdout="", stderr="unknown revision")
        if cmd[:2] == ["git", "merge-base"]:
            if self.merge_base_returncode is not None:
                return ExecResult(returncode=self.merge_base_returncode, stdout="", stderr="")
            code = 0 if (cmd[3], cmd[4]) in self.ancestors else 1
            return ExecResult(returncode=code, stdout="", stderr="")
        if cmd[:2] == ["git", "tag"]:
            return ExecResult(returncode=0, stdout="\n".join(self.tags.get(cmd[3], [])), stderr="")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return ExecResult(returncode=0, stdout="origin/master\n", stderr="")
        if cmd[:2] == ["git", "for-each-ref"]:
            return _LISTING
        return ExecResult(returncode=0, stdout="", stderr="")


def _operator() -> OperatorConfig:
    config = OperatorConfig()
    config.agent_cli_reviewers = [AgentCLIReviewerConfig(name="claude", cli_path="claude")]
    # So the fork resolves to holdenk/<repo> for the PR-lookup path.
    config.github_username = "holdenk"
    return config


def _run_with(executor: _Executor, fn: Any, *args: Any) -> Any:
    with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
        return fn(*args, _operator())


@pytest.mark.django_db
class TestShaResolution:
    def test_the_agent_branch_tip_ancestor_of_master_is_merged(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(
            project=project, fix_branch="bug_7-x", fix_branch_sha="abc123"
        )
        executor = _Executor(shas=("abc123",), ancestors=(("abc123", "origin/master"),))

        run = _run_with(executor, check_project_fixes, project)

        report.refresh_from_db()
        assert run.merged == 1
        assert report.fix_landed_status == "merged"
        assert report.fix_landed_method == "git"
        assert report.fix_landed_detail["branches"] == ["master"]
        assert report.fix_landed_checked_at is not None

    def test_a_commit_in_a_release_tag_is_released(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, fix_branch_sha="abc123")
        executor = _Executor(shas=("abc123",), tags={"abc123": ["v3.5.1", "v4.0.0"]})

        run = _run_with(executor, check_project_fixes, project)

        report.refresh_from_db()
        assert run.released == 1
        assert report.fix_landed_status == "released"
        assert report.fix_landed_detail["tag_count"] == 2
        # No ancestry calls are needed once a tag answers.
        assert not any(c[:2] == ["git", "merge-base"] for c in executor.calls)

    def test_a_commit_on_no_branch_is_not_merged(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, fix_branch_sha="abc123")
        executor = _Executor(shas=("abc123",))

        run = _run_with(executor, check_project_fixes, project)

        report.refresh_from_db()
        assert run.not_merged == 1
        assert report.fix_landed_status == "not-merged"

    def test_a_sha_not_in_the_checkout_falls_through_to_the_branch(self) -> None:
        """A squash-merged branch tip is not in upstream history; the branch
        name (and then the PR lookup) is the fallback, not a non-answer."""
        project = ProjectFactory()
        report = SecurityReportFactory(
            project=project, fix_branch="bug_7-x", fix_branch_sha="abc123"
        )
        executor = _Executor(
            refs={"origin/bug_7-x": "def456"}, ancestors=(("def456", "origin/master"),)
        )

        run = _run_with(executor, check_project_fixes, project)

        report.refresh_from_db()
        assert run.merged == 1
        assert report.fix_landed_detail["checked_ref"] == "def456"


@pytest.mark.django_db
class TestNeverOverwrite:
    def test_indeterminate_never_replaces_a_verdict(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(
            project=project, fix_landed_status="merged", fixed_in_branch="master"
        )

        run = _run_with(_Executor(), check_project_fixes, project)

        report.refresh_from_db()
        assert run.indeterminate == 1  # the check happened and found nothing provable
        assert report.fix_landed_status == "merged"  # but the verdict stayed

    def test_released_is_terminal(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, fix_landed_status="released")

        run = _run_with(_Executor(), check_project_fixes, project)

        report.refresh_from_db()
        assert run.reports_considered == 0  # released rows are not even re-checked
        assert report.fix_landed_status == "released"

    def test_a_merge_base_error_is_not_a_verdict(self) -> None:
        """Exit 128 is "couldn't parse a rev", not "not an ancestor" — the
        scan_already_fixed rule about git exit codes."""
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, fix_branch_sha="abc123")
        executor = _Executor(shas=("abc123",), merge_base_returncode=128)

        run = _run_with(executor, check_project_fixes, project)

        report.refresh_from_db()
        assert run.indeterminate == 1
        assert report.fix_landed_status == "indeterminate"
        assert "failed" in report.fix_landed_detail["note"]


@pytest.mark.django_db
class TestFixedInBranchText:
    def test_a_topic_branch_on_origin_is_tested(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, fixed_in_branch="fix-cve-2025-12345")
        executor = _Executor(
            refs={"origin/fix-cve-2025-12345": "aaa999"},
            ancestors=(("aaa999", "origin/branch-3.5"),),
        )

        run = _run_with(executor, check_project_fixes, project)

        report.refresh_from_db()
        assert run.merged == 1
        assert report.fix_landed_detail["branches"] == ["branch-3.5"]

    def test_a_mainline_branch_name_proves_nothing(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, fixed_in_branch="master")

        run = _run_with(_Executor(), check_project_fixes, project)

        report.refresh_from_db()
        assert run.indeterminate == 1
        assert report.fix_landed_status == "indeterminate"
        assert "mainline" in report.fix_landed_detail["note"]

    def test_a_free_text_list_tests_each_ref_and_takes_the_strongest(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(
            project=project, fixed_in_branch="master, fix-cve-2025-12345"
        )
        executor = _Executor(refs={"origin/fix-cve-2025-12345": "aaa999"})

        run = _run_with(executor, check_project_fixes, project)

        report.refresh_from_db()
        assert run.not_merged == 1
        assert report.fix_landed_detail["checked_ref"] == "aaa999"

    def test_no_branch_anywhere_is_no_fix_branch(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, fixed_in_branch="", fix_branch="")
        # The sweep only selects branch-carrying rows, so exercise the per-report path.
        note = _run_with(_Executor(), check_fix_landed, report)

        report.refresh_from_db()
        assert note.startswith("no-fix-branch")
        assert report.fix_landed_status == "no-fix-branch"


@pytest.mark.django_db
class TestPRLookup:
    def test_a_merged_pr_tests_its_merge_commit(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(project=project, fix_branch="bug_7-x")
        executor = _Executor(shas=("merge999",), ancestors=(("merge999", "origin/master"),))
        pr = {
            "html_url": "https://github.com/apache/spark/pull/1",
            "merged_at": "2026-09-01T00:00:00Z",
            "merge_commit_sha": "merge999",
        }
        with patch("franktheunicorn.security.fix_landed._find_pr", return_value=pr):
            run = _run_with(executor, check_project_fixes, project)

        report.refresh_from_db()
        assert run.merged == 1
        assert report.fix_landed_detail["pr_url"].endswith("/pull/1")
        assert "merge commit" in report.fix_landed_detail["note"]

    def test_an_unmerged_pr_is_not_merged(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(project=project, fix_branch="bug_7-x")
        pr = {"html_url": "https://github.com/apache/spark/pull/1", "merged_at": None}
        with patch("franktheunicorn.security.fix_landed._find_pr", return_value=pr):
            run = _run_with(_Executor(), check_project_fixes, project)

        report.refresh_from_db()
        assert run.not_merged == 1
        assert report.fix_landed_status == "not-merged"

    def test_no_pr_found_is_indeterminate(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(project=project, fix_branch="bug_7-x")
        with patch("franktheunicorn.security.fix_landed._find_pr", return_value={}):
            run = _run_with(_Executor(), check_project_fixes, project)

        report.refresh_from_db()
        assert run.indeterminate == 1
        assert "no upstream PR" in report.fix_landed_detail["note"]

    def test_a_failed_lookup_is_indeterminate_not_silence(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(project=project, fix_branch="bug_7-x")
        with patch("franktheunicorn.security.fix_landed._find_pr", return_value=None):
            run = _run_with(_Executor(), check_project_fixes, project)

        report.refresh_from_db()
        assert run.indeterminate == 1
        assert "failed" in report.fix_landed_detail["note"]


@pytest.mark.django_db
class TestFindPr:
    def _response(self, status: int, payload: Any) -> Any:
        import httpx

        return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://x"))

    def test_picks_the_merged_pr(self) -> None:
        pulls = [
            {"merged_at": None, "html_url": "https://x/1"},
            {"merged_at": "2026-09-01T00:00:00Z", "html_url": "https://x/2"},
        ]
        with patch(
            "franktheunicorn.security.fix_landed.httpx.get",
            return_value=self._response(200, pulls),
        ) as get:
            pr = _find_pr("apache/spark", "holdenk", "bug_7-x", _operator())

        assert pr is not None and pr["html_url"] == "https://x/2"
        assert get.call_args.kwargs["params"]["head"] == "holdenk:bug_7-x"

    def test_no_pulls_is_an_empty_answer_not_a_failure(self) -> None:
        with patch(
            "franktheunicorn.security.fix_landed.httpx.get",
            return_value=self._response(200, []),
        ):
            assert _find_pr("apache/spark", "holdenk", "bug_7-x", _operator()) == {}

    def test_a_non_200_is_a_failure_not_an_empty_answer(self) -> None:
        with patch(
            "franktheunicorn.security.fix_landed.httpx.get",
            return_value=self._response(403, {"message": "rate limited"}),
        ):
            assert _find_pr("apache/spark", "holdenk", "bug_7-x", _operator()) is None

    def test_the_operator_token_goes_out_when_configured(self) -> None:
        config = _operator()
        config.github_token = "ghp_test"
        with patch(
            "franktheunicorn.security.fix_landed.httpx.get",
            return_value=self._response(200, []),
        ) as get:
            _find_pr("apache/spark", "holdenk", "bug_7-x", config)

        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer ghp_test"


@pytest.mark.django_db
class TestSweep:
    def test_reports_without_a_branch_are_not_considered(self) -> None:
        project = ProjectFactory()
        SecurityReportFactory(project=project)
        SecurityReportFactory(project=project, status="invalid", fix_branch_sha="abc123")

        run = _run_with(_Executor(), check_project_fixes, project)

        assert run.error == ""
        assert run.reports_considered == 0

    def test_a_project_with_nothing_to_check_says_so(self) -> None:
        project = ProjectFactory()

        run = _run_with(_Executor(), check_project_fixes, project)

        assert run.error == ""
        assert run.reports_considered == 0

    def test_without_a_checkout_the_error_is_carried(self) -> None:
        project = ProjectFactory()
        SecurityReportFactory(project=project, fix_branch_sha="abc123")
        config = _operator()
        config.security_triage.verifier.enabled = False

        run = check_project_fixes(project, config)

        assert "verifier.enabled" in run.error
        assert "did not run" in run.summary()

    def test_the_per_report_check_returns_a_note(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, fix_branch_sha="abc123")
        executor = _Executor(shas=("abc123",), ancestors=(("abc123", "origin/master"),))

        note = _run_with(executor, check_fix_landed, report)

        assert note.startswith("merged")
        report.refresh_from_db()
        assert report.fix_landed_status == "merged"


@pytest.mark.django_db
class TestCveFixesComposite:
    """The CVE button's sweep: the match half must run first, because the tie
    is what gives the landed half a ref to test."""

    def test_match_runs_before_the_landed_check(self) -> None:
        from franktheunicorn.security.branch_scan import BranchMatchRun
        from franktheunicorn.security.fix_landed import FixLandedRun

        project = ProjectFactory()
        order: list[str] = []

        def _match(p: Any, _c: Any) -> BranchMatchRun:
            order.append("match")
            return BranchMatchRun(project=p.full_name, applied=1)

        def _check(p: Any, _c: Any) -> FixLandedRun:
            order.append("check")
            return FixLandedRun(project=p.full_name, merged=1)

        with (
            patch(
                "franktheunicorn.security.branch_scan.match_fix_branches",
                side_effect=_match,
            ),
            patch(
                "franktheunicorn.security.fix_landed.check_project_fixes",
                side_effect=_check,
            ),
        ):
            run = check_cve_fixes(project, _operator())

        assert order == ["match", "check"]
        assert run.error == ""
        assert "1 branch(es) recorded" in run.summary()
        assert "1 merged" in run.summary()

    def test_a_match_failure_short_circuits_the_landed_check(self) -> None:
        """No checkout fails both halves the same way; saying it once is enough."""
        from franktheunicorn.security.branch_scan import BranchMatchRun

        project = ProjectFactory()
        with (
            patch(
                "franktheunicorn.security.branch_scan.match_fix_branches",
                return_value=BranchMatchRun(project=project.full_name, error="no checkout"),
            ),
            patch("franktheunicorn.security.fix_landed.check_project_fixes") as landed,
        ):
            run = check_cve_fixes(project, _operator())

        assert run.error == "no checkout"
        assert "did not run" in run.summary()
        assert not landed.called
