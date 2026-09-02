"""Tests for the two git-only backlog sweeps.

The properties worth pinning are about what gets written and what doesn't. An
automatic branch tie is only defensible if it never lands on top of the
operator's own answer and never fires off a weak hint; a "likely fixed" verdict
is only worth having if a patch that neither applies nor reverse-applies comes
back as unclear rather than rounding to good news.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import (
    AgentCLIReviewerConfig,
    OperatorConfig,
    SecurityBranchScanConfig,
)
from franktheunicorn.core.models import SecurityReport
from franktheunicorn.review.tool_executor import ExecResult
from franktheunicorn.security.branch_scan import (
    AUTO_TIE_CONFIDENCE,
    BranchEvidence,
    _Candidate,
    gather_branch_evidence,
    list_origin_branches,
    match_fix_branches,
    projects_with_open_reports,
    scan_already_fixed,
    score_branch,
)
from tests.factories import ProjectFactory, SecurityReportFactory

_PATCH = "--- a/sql/core/Foo.scala\n+++ b/sql/core/Foo.scala\n@@ -1 +1 @@\n-old\n+new\n"

#: Two version branches, a topic branch that names a CVE, and one that last saw
#: a commit in 2017 — old enough that the activity window drops it.
_LISTING = ExecResult(
    returncode=0,
    stdout=(
        "origin/HEAD 1900000000\n"
        "origin/master 1900000000\n"
        "origin/fix-cve-2025-12345 1899000000\n"
        "origin/branch-3.5 1898000000\n"
        "origin/ancient-topic 1500000000\n"
    ),
    stderr="",
)


def _operator(**scan_overrides: Any) -> OperatorConfig:
    config = OperatorConfig()
    config.agent_cli_reviewers = [AgentCLIReviewerConfig(name="claude", cli_path="claude")]
    config.security_triage.branch_scan = SecurityBranchScanConfig(**scan_overrides)
    return config


class _GitExecutor:
    """Scripted git. Keyed on the argv shape plus whichever rev it names."""

    def __init__(self, responses: dict[str, ExecResult | None] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[list[str]] = []
        self.checked_out: list[str] = []

    def prepare_repo(self, owner: str, repo: str, **kwargs: Any) -> str | None:
        return "/w/spark"

    def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        self.calls.append(cmd)
        if cmd[:2] == ["git", "checkout"]:
            self.checked_out.append(cmd[-1])
            return ExecResult(returncode=0, stdout="deadbeef", stderr="")
        key = " ".join(cmd)
        for needle, response in self.responses.items():
            if needle in key:
                return response
        if cmd[:2] == ["git", "rev-parse"]:
            return ExecResult(returncode=0, stdout="deadbeef\n", stderr="")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return ExecResult(returncode=0, stdout="origin/master\n", stderr="")
        if cmd[:2] == ["git", "for-each-ref"]:
            return _LISTING
        return ExecResult(returncode=0, stdout="", stderr="")


def _run_with(executor: _GitExecutor, fn: Any, *args: Any) -> Any:
    with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
        return fn(*args)


class TestBranchListing:
    """Which branches are candidates, and why not the verifier's list."""

    def test_lists_every_recent_branch_not_just_version_branches(self) -> None:
        """The whole point: a fix lives on a topic branch, which
        ``select_branches`` filters out by design."""
        branches = list_origin_branches(
            _GitExecutor(), "/w/spark", SecurityBranchScanConfig(), "master"
        )

        assert "fix-cve-2025-12345" in branches
        assert "master" in branches
        assert "branch-3.5" in branches

    def test_head_is_not_a_branch(self) -> None:
        assert "HEAD" not in list_origin_branches(
            _GitExecutor(), "/w/spark", SecurityBranchScanConfig(), "master"
        )

    def test_a_long_dead_branch_is_not_scanned(self) -> None:
        branches = list_origin_branches(
            _GitExecutor(), "/w/spark", SecurityBranchScanConfig(), "master"
        )
        assert "ancient-topic" not in branches

    def test_the_cap_bounds_the_walk(self) -> None:
        branches = list_origin_branches(
            _GitExecutor(), "/w/spark", SecurityBranchScanConfig(max_branches=2), "master"
        )
        # master is seeded and exempt, so the cap counts the other two.
        assert len(branches) == 3

    def test_the_default_branch_is_never_the_one_dropped(self) -> None:
        """`select_branches` seeds it and exempts it from the cap; the copy here
        dropped both along with the name filter. A repo with 300 live dependabot
        branches then fills every slot ahead of a master committed yesterday, so
        `max_default_commits` never runs and the fix that landed on master three
        months ago is never looked at."""
        branches = list_origin_branches(
            _GitExecutor(), "/w/spark", SecurityBranchScanConfig(max_branches=1), "master"
        )

        assert branches[0] == "master"
        assert len(branches) == 2  # master, exempt, plus the one capped slot

    def test_a_default_branch_older_than_the_window_is_still_scanned(self) -> None:
        executor = _GitExecutor({"for-each-ref": ExecResult(0, "origin/dormant 1500000000\n", "")})
        branches = list_origin_branches(executor, "/w/spark", SecurityBranchScanConfig(), "dormant")
        assert branches == ["dormant"]

    def test_a_failed_listing_is_empty_not_a_crash(self) -> None:
        executor = _GitExecutor({"for-each-ref": ExecResult(1, "", "not a repo")})
        assert (
            list_origin_branches(executor, "/w/spark", SecurityBranchScanConfig(), "master") == []
        )


class TestEvidence:
    """What one branch contributes, and what the default branch deliberately doesn't."""

    def _executor(self) -> _GitExecutor:
        return _GitExecutor(
            {
                "--format=%s%n%b%n": ExecResult(
                    0, "Tighten Foo validation\n\nFixes CVE-2025-12345 and f0012.\n\x1e", ""
                ),
                "--name-only": ExecResult(0, "sql/core/Foo.scala\nsql/core/Bar.scala\n", ""),
            }
        )

    def test_ids_are_indexed_from_the_message_and_the_name(self) -> None:
        evidence = gather_branch_evidence(
            self._executor(),
            "/w/spark",
            "fix-cve-2025-12345",
            "master",
            SecurityBranchScanConfig(),
        )

        assert "cve-2025-12345" in evidence.name_cves
        assert "f0012" in evidence.tokens
        assert evidence.paths == {"sql/core/Foo.scala", "sql/core/Bar.scala"}

    def test_the_default_branch_contributes_no_paths(self) -> None:
        """Its range is thousands of commits, so its path set is most of the repo
        — which matches every report and is therefore evidence about none."""
        evidence = gather_branch_evidence(
            self._executor(), "/w/spark", "master", "master", SecurityBranchScanConfig()
        )

        assert evidence.paths == set()
        assert "cve-2025-12345" in evidence.cves

    def test_merge_commits_are_read(self) -> None:
        """Verified against a throwaway repo: merge a topic branch with "Merge pull
        request #42 ... / Fix CVE-2026-1234", and `--no-merges` makes that CVE
        absent from master's log entirely — it lives in the merge commit. The flag
        hid exactly the sentence a maintainer writes when landing a security fix,
        on every repo using the ordinary GitHub flow."""
        executor = self._executor()
        gather_branch_evidence(executor, "/w/spark", "master", "master", SecurityBranchScanConfig())

        logs = [c for c in executor.calls if c[:2] == ["git", "log"]]
        assert logs
        assert not any("--no-merges" in c for c in logs)

    def test_a_path_set_that_hit_the_cap_is_dropped_not_truncated(self) -> None:
        """A long-lived release branch touches most of a subtree, so its path set
        overlaps nearly every report and every row renders the same suggestion. A
        set that wide is evidence about nothing, and a truncated one is arbitrary
        as well as wide."""
        executor = _GitExecutor(
            {
                "--format=%s%n%b%n": ExecResult(0, "work\n\x1e", ""),
                "--name-only": ExecResult(0, "".join(f"f{n}.scala\n" for n in range(50)), ""),
            }
        )
        evidence = gather_branch_evidence(
            executor,
            "/w/spark",
            "branch-3.5",
            "master",
            SecurityBranchScanConfig(max_paths_per_branch=10),
        )

        assert evidence.paths == set()

    def test_a_failed_log_loses_the_branch_not_the_run(self) -> None:
        executor = _GitExecutor({"--format=%s%n%b%n": ExecResult(128, "", "bad revision")})
        evidence = gather_branch_evidence(
            executor, "/w/spark", "gone", "master", SecurityBranchScanConfig()
        )
        assert evidence.messages == []


class TestScoring:
    """Which signals are allowed to become an answer by themselves."""

    def _candidate(self, **overrides: Any) -> _Candidate:
        base: dict[str, Any] = {
            "pk": 1,
            "cve": "CVE-2025-12345",
            "finding": "f0012",
            "fix_branch": "",
            "paths": {"sql/core/Foo.scala"},
        }
        base.update(overrides)
        return _Candidate(**base)

    def test_our_own_pushed_branch_wins(self) -> None:
        match = score_branch(
            self._candidate(fix_branch="tighten-foo"), BranchEvidence(name="tighten-foo")
        )
        assert match is not None
        assert match.confidence >= AUTO_TIE_CONFIDENCE
        assert "fix agent" in match.reason

    def test_a_cve_in_the_branch_name_is_enough_to_tie(self) -> None:
        evidence = BranchEvidence(name="fix-cve-2025-12345", name_cves={"cve-2025-12345"})
        evidence.cves = {"cve-2025-12345"}
        match = score_branch(self._candidate(), evidence)

        assert match is not None
        assert match.confidence >= AUTO_TIE_CONFIDENCE
        assert match.reason == "branch name contains CVE-2025-12345"

    def test_a_cve_in_a_commit_message_is_strong_but_not_an_answer(self) -> None:
        """Mentioning is not fixing. "Add a regression test for CVE-x" and "Revert
        the CVE-x fix, it broke the build" both put the id in the message index,
        and at auto-tie strength both wrote fixed_in_branch for an open hole."""
        evidence = BranchEvidence(
            name="topic",
            messages=["tighten foo validation\n\nfixes cve-2025-12345."],
            cves={"cve-2025-12345"},
        )
        match = score_branch(self._candidate(), evidence)

        assert match is not None
        assert match.confidence < AUTO_TIE_CONFIDENCE
        assert "tighten foo validation" in match.reason

    def test_a_revert_naming_a_cve_cannot_auto_tie(self) -> None:
        evidence = BranchEvidence(
            name="topic",
            messages=["revert the cve-2025-12345 fix, it broke the build"],
            cves={"cve-2025-12345"},
        )
        match = score_branch(self._candidate(), evidence)

        assert match is not None
        assert match.confidence < AUTO_TIE_CONFIDENCE

    def test_only_a_named_branch_or_our_own_push_reaches_the_threshold(self) -> None:
        """The two signals allowed to become an answer, pinned so a future tier
        can't quietly join them."""
        named = BranchEvidence(name="fix-cve-2025-12345", name_cves={"cve-2025-12345"})
        named.cves = {"cve-2025-12345"}
        ours = BranchEvidence(name="tighten-foo")

        assert score_branch(self._candidate(), named).confidence >= AUTO_TIE_CONFIDENCE  # type: ignore[union-attr]
        assert (
            score_branch(self._candidate(fix_branch="tighten-foo"), ours).confidence  # type: ignore[union-attr]
            >= AUTO_TIE_CONFIDENCE
        )
        for weaker in (
            BranchEvidence(name="topic", tokens={"f0012"}),
            BranchEvidence(name="fix-f0012", name_tokens={"fix", "f0012"}),
            BranchEvidence(name="refactor", paths={"sql/core/Foo.scala"}),
        ):
            match = score_branch(self._candidate(), weaker)
            assert match is None or match.confidence < AUTO_TIE_CONFIDENCE

    def test_a_short_finding_id_never_reaches_scoring(self) -> None:
        """``f1`` matches a token on every branch in the repo, so
        ``_candidates_for`` drops it before it can. Here: an empty finding is
        simply not a signal."""
        evidence = BranchEvidence(name="topic", tokens={"f1"})
        assert score_branch(self._candidate(cve="", finding="", paths=set()), evidence) is None

    def test_path_overlap_is_a_hint_and_never_an_answer(self) -> None:
        evidence = BranchEvidence(name="refactor-sql", paths={"sql/core/Foo.scala"})
        match = score_branch(self._candidate(cve="", finding=""), evidence)

        assert match is not None
        assert match.confidence < AUTO_TIE_CONFIDENCE
        assert "sql/core/Foo.scala" in match.reason

    def test_more_overlapping_files_still_does_not_reach_the_tie_threshold(self) -> None:
        """A branch that rewrote the whole package touches all of them and fixes none."""
        paths = {f"sql/core/F{n}.scala" for n in range(20)}
        evidence = BranchEvidence(name="big-refactor", paths=paths)
        match = score_branch(self._candidate(cve="", finding="", paths=paths), evidence)

        assert match is not None
        assert match.confidence < AUTO_TIE_CONFIDENCE

    def test_nothing_in_common_is_no_match(self) -> None:
        assert score_branch(self._candidate(), BranchEvidence(name="unrelated")) is None


@pytest.mark.django_db
class TestMatchFixBranches:
    """The sweep that writes ``fixed_in_branch``, and what stops it."""

    def _executor(self, message: str = "") -> _GitExecutor:
        return _GitExecutor(
            {
                "--format=%s%n%b%n": ExecResult(0, f"{message}\n\x1e" if message else "", ""),
                "--name-only": ExecResult(0, "", ""),
            }
        )

    def test_a_cve_named_branch_is_tied_in_automatically(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, matched_cve_id="CVE-2025-12345")

        run = _run_with(self._executor(), match_fix_branches, project, _operator())

        report.refresh_from_db()
        assert run.applied == 1
        assert report.fixed_in_branch == "fix-cve-2025-12345"
        assert report.branch_match_applied is True
        assert report.branch_match_confidence is not None
        assert "CVE-2025-12345" in report.branch_match_reason

    def test_a_weak_match_is_a_suggestion_and_not_the_answer(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(
            project=project,
            matched_cve_id="",
            finding_id="",
            raw_text="hole in sql/core/Foo.scala",
            proposed_patch="",
            title="",
            parsed_component="",
            parsed_poc="",
        )
        executor = self._executor()
        executor.responses["--name-only"] = ExecResult(0, "sql/core/Foo.scala\n", "")

        run = _run_with(executor, match_fix_branches, project, _operator())

        report.refresh_from_db()
        assert run.matched == 1
        assert run.applied == 0
        assert report.fixed_in_branch == ""
        assert report.branch_match_branch != ""
        assert report.branch_match_applied is False

    def test_the_operators_own_answer_is_never_overwritten(self) -> None:
        project = ProjectFactory()
        report = SecurityReportFactory(
            project=project, matched_cve_id="CVE-2025-12345", fixed_in_branch="branch-3.5"
        )

        run = _run_with(self._executor(), match_fix_branches, project, _operator())

        report.refresh_from_db()
        assert report.fixed_in_branch == "branch-3.5"
        assert report.branch_match_branch == ""
        assert run.reports_considered == 0

    def test_an_answer_typed_mid_sweep_still_wins(self) -> None:
        """The guard that matters, and the version of this test that only set the
        field up front never reached it: `_candidates_for` filtered the report out
        at SELECT time, so the `.update()` race guard was never evaluated at all.
        A sweep over a few hundred reports takes real wall-clock time, and this is
        the operator typing at minute three into a list built at minute zero."""
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, matched_cve_id="CVE-2025-12345")

        def _operator_types(*args: Any, **kwargs: Any) -> BranchEvidence:
            SecurityReport.objects.filter(pk=report.pk).update(fixed_in_branch="branch-3.5")
            return BranchEvidence(name="fix-cve-2025-12345", name_cves={"cve-2025-12345"})

        with patch(
            "franktheunicorn.security.branch_scan.gather_branch_evidence",
            side_effect=_operator_types,
        ):
            run = _run_with(self._executor(), match_fix_branches, project, _operator())

        report.refresh_from_db()
        assert report.fixed_in_branch == "branch-3.5"
        assert report.branch_match_branch == ""
        assert run.applied == 0

    def test_a_verdict_typed_mid_sweep_stops_the_write(self) -> None:
        """`fixed_in_branch` stays empty when the operator rules `invalid`, so the
        branch filter alone let a fix branch land on a report explicitly ruled
        not-a-vulnerability."""
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, matched_cve_id="CVE-2025-12345")

        def _operator_rules(*args: Any, **kwargs: Any) -> BranchEvidence:
            SecurityReport.objects.filter(pk=report.pk).update(status="invalid")
            return BranchEvidence(name="fix-cve-2025-12345", name_cves={"cve-2025-12345"})

        with patch(
            "franktheunicorn.security.branch_scan.gather_branch_evidence",
            side_effect=_operator_rules,
        ):
            _run_with(self._executor(), match_fix_branches, project, _operator())

        report.refresh_from_db()
        assert report.fixed_in_branch == ""
        assert report.branch_match_applied is False

    def test_a_cleared_branch_is_a_rejection_and_does_not_come_back(self) -> None:
        """Clearing the field is the documented way to reject a branch. Without a
        record of the attempt the same name scores the same 0.95 next run."""
        project = ProjectFactory()
        report = SecurityReportFactory(
            project=project,
            matched_cve_id="CVE-2025-12345",
            branch_match_branch="fix-cve-2025-12345",
            branch_match_applied=True,
            fixed_in_branch="",
        )

        run = _run_with(self._executor(), match_fix_branches, project, _operator())

        report.refresh_from_db()
        assert report.fixed_in_branch == ""
        assert run.applied == 0
        # Still recorded as a suggestion — we don't pretend we found nothing.
        assert report.branch_match_branch == "fix-cve-2025-12345"

    def test_an_auto_tie_is_not_the_operator_having_ruled(self) -> None:
        """It flips fixed_in_branch, which every bulk path reads through
        operator_has_ruled — so without the exception a heuristic match silently
        retires an untriaged report and the UI reports it as operator-ruled."""
        project = ProjectFactory()
        report = SecurityReportFactory(project=project, matched_cve_id="")

        _run_with(self._executor(), match_fix_branches, project, _operator())
        report.refresh_from_db()
        assert report.operator_has_ruled is False

        report.branch_match_applied = True
        report.fixed_in_branch = "fix-cve-2025-12345"
        report.save()
        assert report.operator_has_ruled is False
        assert not SecurityReport.objects.filter(
            SecurityReport.operator_has_ruled_q(), pk=report.pk
        ).exists()

        # The operator's own typing still counts, which is the whole point.
        report.branch_match_applied = False
        report.save()
        assert report.operator_has_ruled is True
        assert SecurityReport.objects.filter(
            SecurityReport.operator_has_ruled_q(), pk=report.pk
        ).exists()

    def test_a_report_owed_no_fix_is_not_in_the_set(self) -> None:
        project = ProjectFactory()
        SecurityReportFactory(project=project, status="invalid", matched_cve_id="CVE-2025-12345")

        run = _run_with(self._executor(), match_fix_branches, project, _operator())
        assert run.reports_considered == 0

    def test_no_checkout_is_an_error_not_a_silent_zero(self) -> None:
        project = ProjectFactory()
        SecurityReportFactory(project=project)
        operator = _operator()
        operator.agent_cli_reviewers = []

        run = _run_with(self._executor(), match_fix_branches, project, operator)

        assert "agent_cli_reviewers" in run.error
        assert run.matched == 0

    def test_a_failed_fetch_is_carried_to_the_operator(self) -> None:
        project = ProjectFactory()
        SecurityReportFactory(project=project, matched_cve_id="CVE-2025-12345")
        executor = self._executor()
        executor.responses["git fetch"] = ExecResult(1, "", "could not resolve host")

        run = _run_with(executor, match_fix_branches, project, _operator())

        assert run.stale_warning
        assert "Could not fetch origin" in run.summary()
        # Still answered from what the checkout had — refusing gives nothing.
        assert run.applied == 1


@pytest.mark.django_db
class TestScanAlreadyFixed:
    """The reverse-apply sweep. Proof, when it can be, and honest when it can't."""

    def _report(self, project: Any, **overrides: Any) -> SecurityReport:
        base: dict[str, Any] = {"project": project, "proposed_patch": _PATCH, "status": "new"}
        base.update(overrides)
        return SecurityReportFactory(**base)

    def _executor(self, *, reverse_ok: bool, forward_ok: bool = False) -> _GitExecutor:
        """git apply travels inside an ``sh -c`` script, so the flag is in the text."""

        class _ApplyExecutor(_GitExecutor):
            def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
                if cmd[:2] == ["sh", "-c"]:
                    self.calls.append(cmd)
                    ok = reverse_ok if " -R " in cmd[2] else forward_ok
                    return ExecResult(returncode=0 if ok else 1, stdout="", stderr="")
                return super().run(cmd, cwd, timeout, stdin)

        return _ApplyExecutor()

    def test_a_patch_that_reverse_applies_is_already_fixed(self) -> None:
        project = ProjectFactory()
        report = self._report(project)

        run = _run_with(self._executor(reverse_ok=True), scan_already_fixed, project, _operator())

        report.refresh_from_db()
        assert run.fixed == 1
        assert report.recheck_status == "likely-fixed"
        assert report.recheck_method == "git"
        assert "reverse-applies" in report.recheck_reason

    def test_a_patch_that_still_applies_is_still_valid(self) -> None:
        project = ProjectFactory()
        report = self._report(project)

        run = _run_with(
            self._executor(reverse_ok=False, forward_ok=True),
            scan_already_fixed,
            project,
            _operator(),
        )

        report.refresh_from_db()
        assert run.still_valid == 1
        assert report.recheck_status == "still-valid"

    def test_neither_direction_is_unclear_not_good_news(self) -> None:
        project = ProjectFactory()
        report = self._report(project)

        run = _run_with(
            self._executor(reverse_ok=False, forward_ok=False),
            scan_already_fixed,
            project,
            _operator(),
        )

        report.refresh_from_db()
        assert run.unclear == 1
        assert report.recheck_status == "unclear"
        assert "the code moved" in report.recheck_reason

    def test_a_report_with_no_patch_is_skipped_not_answered(self) -> None:
        project = ProjectFactory()
        report = self._report(project, proposed_patch="")

        run = _run_with(self._executor(reverse_ok=True), scan_already_fixed, project, _operator())

        report.refresh_from_db()
        assert run.reports_considered == 0
        assert report.recheck_status == ""

    def test_the_branch_the_report_is_about_is_the_one_checked(self) -> None:
        """A hole reported against branch-3.5 is not fixed by a commit on master."""
        project = ProjectFactory()
        self._report(project, fix_base_branch="branch-3.5")
        executor = self._executor(reverse_ok=True)

        _run_with(executor, scan_already_fixed, project, _operator())

        assert executor.checked_out == ["origin/branch-3.5"]

    def test_reports_sharing_a_branch_are_checked_out_once(self) -> None:
        project = ProjectFactory()
        for _ in range(3):
            self._report(project)
        executor = self._executor(reverse_ok=True)

        _run_with(executor, scan_already_fixed, project, _operator())

        assert executor.checked_out == ["origin/master"]

    def test_an_operator_ruled_report_is_left_alone(self) -> None:
        project = ProjectFactory()
        report = self._report(project, status="invalid")

        run = _run_with(self._executor(reverse_ok=True), scan_already_fixed, project, _operator())

        report.refresh_from_db()
        assert run.reports_considered == 0
        assert report.recheck_status == ""

    def test_a_verdict_typed_mid_sweep_stops_the_write(self) -> None:
        """The status re-test in the `.update()`, which the test above never
        reaches — `_fixed_scan_candidates` excludes `invalid` at SELECT time."""
        project = ProjectFactory()
        report = self._report(project)
        executor = self._executor(reverse_ok=True)

        def _rule_invalid(*args: Any, **kwargs: Any) -> bool | None:
            SecurityReport.objects.filter(pk=report.pk).update(status="invalid")
            return True

        with patch(
            "franktheunicorn.security.branch_scan.patch_apply_check", side_effect=_rule_invalid
        ):
            run = _run_with(executor, scan_already_fixed, project, _operator())

        report.refresh_from_db()
        assert report.recheck_status == ""
        assert run.fixed == 0

    def test_a_non_diff_patch_is_skipped_not_called_moved(self) -> None:
        """git exits 128 for input it can't parse as a diff, which `ExecResult.ok`
        cannot tell from exit 1. Reading the bool alone reported "the code moved"
        for a patch git never read."""
        project = ProjectFactory()
        report = self._report(project, proposed_patch="This is a description, not a patch.\n")

        class _Unparseable(_GitExecutor):
            def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
                if cmd[:2] == ["sh", "-c"]:
                    return ExecResult(returncode=128, stdout="", stderr="no valid patches in input")
                return super().run(cmd, cwd, timeout, stdin)

        run = _run_with(_Unparseable(), scan_already_fixed, project, _operator())

        report.refresh_from_db()
        assert report.recheck_status == ""
        assert run.unclear == 0
        assert any("could not read it as a patch" in reason for reason in run.skipped)

    def test_a_dropped_executor_on_the_forward_check_is_not_a_verdict(self) -> None:
        """The forward call sat in an `elif`, so its None — an SSH drop or a
        timeout — fell through to "the code moved" and stamped a fabricated git
        verdict on every remaining report in the group."""
        project = ProjectFactory()
        report = self._report(project)
        answers = iter([False, None])

        with patch(
            "franktheunicorn.security.branch_scan.patch_apply_check",
            side_effect=lambda *a, **k: next(answers),
        ):
            run = _run_with(
                self._executor(reverse_ok=False), scan_already_fixed, project, _operator()
            )

        report.refresh_from_db()
        assert report.recheck_status == ""
        assert run.unclear == 0

    def test_unclear_does_not_overwrite_a_paid_agent_verdict(self) -> None:
        """A non-answer must not replace an answer somebody paid a cloud agent for,
        along with its commit citation."""
        project = ProjectFactory()
        report = self._report(
            project,
            recheck_status="likely-fixed",
            recheck_reason="commit abc123 closes it",
            recheck_method="agent",
        )

        run = _run_with(
            self._executor(reverse_ok=False, forward_ok=False),
            scan_already_fixed,
            project,
            _operator(),
        )

        report.refresh_from_db()
        assert report.recheck_status == "likely-fixed"
        assert report.recheck_method == "agent"
        assert run.unclear == 0

    def test_a_real_git_answer_does_replace_the_agents_guess(self) -> None:
        """Proof beats a judgement call; that asymmetry is the point of the column."""
        project = ProjectFactory()
        report = self._report(project, recheck_status="still-valid", recheck_method="agent")

        run = _run_with(self._executor(reverse_ok=True), scan_already_fixed, project, _operator())

        report.refresh_from_db()
        assert report.recheck_status == "likely-fixed"
        assert report.recheck_method == "git"
        assert run.fixed == 1

    def test_the_archive_name_supplies_the_base_branch(self) -> None:
        """`fix_base_branch` has one writer and it needs a Fix-button press, so for
        the whole imported backlog the archive label is the only signal there is —
        and checking a branch-3.5 patch against master is a wall of "unclear"."""
        project = ProjectFactory()
        self._report(project, fix_base_branch="", source_archive="spark-branch-3.5-findings.zip")
        executor = self._executor(reverse_ok=True)

        _run_with(executor, scan_already_fixed, project, _operator())

        assert executor.checked_out == ["origin/branch-3.5"]

    def test_a_valid_report_is_in_the_set(self) -> None:
        """Wider than the agent recheck on purpose: a report you accepted and
        haven't fixed is exactly the one where "it landed Tuesday" matters."""
        project = ProjectFactory()
        report = self._report(project, status="valid")

        run = _run_with(self._executor(reverse_ok=True), scan_already_fixed, project, _operator())

        report.refresh_from_db()
        assert run.fixed == 1
        assert report.recheck_status == "likely-fixed"

    def test_a_branch_that_cannot_be_checked_out_is_skipped_with_a_reason(self) -> None:
        project = ProjectFactory()
        self._report(project)

        with patch("franktheunicorn.security.branch_scan._checkout", return_value=""):
            run = _run_with(
                self._executor(reverse_ok=True), scan_already_fixed, project, _operator()
            )

        assert run.reports_considered == 0
        assert any("could not be checked out" in reason for reason in run.skipped)


@pytest.mark.django_db
class TestProjectSelection:
    def test_a_project_less_report_gives_the_sweeps_nothing_to_do(self) -> None:
        SecurityReportFactory(project=None)
        assert projects_with_open_reports() == []

    def test_a_project_with_only_closed_reports_is_not_swept(self) -> None:
        project = ProjectFactory()
        SecurityReportFactory(project=project, status="invalid")
        assert projects_with_open_reports() == []

    def test_a_project_with_an_open_report_is_swept_once(self) -> None:
        project = ProjectFactory()
        SecurityReportFactory(project=project)
        SecurityReportFactory(project=project)
        assert projects_with_open_reports() == [project]
