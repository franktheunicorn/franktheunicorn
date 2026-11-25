"""Tests for review/fork_setup.py — why a PR's CI never ran, and who has to fix it."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from django.utils import timezone

from franktheunicorn.config.models import ForkSetupConfig, ProjectConfig
from franktheunicorn.core.models import (
    CIFailureAssessment,
    ForkSetupAssessment,
    OperatorAction,
    Project,
    PullRequest,
)
from franktheunicorn.review import ci_failure, fork_setup
from franktheunicorn.review.fork_setup import (
    SPARK_BUILD_WORKFLOW,
    Facts,
    Problem,
    SetupCheck,
    approve,
    build_workflow_for,
    compose_comment,
    diagnose,
    post_blocker,
    problems_from,
    record_dismissal,
    record_posted,
    restore,
    scan_project,
    touched_workflows,
)
from tests.factories import (
    CIFailureAssessmentFactory,
    ForkSetupAssessmentFactory,
    ProjectFactory,
    PullRequestFactory,
)

SPARK = ProjectConfig(owner="apache", repo="spark")
GENERIC = ProjectConfig(owner="acme", repo="widget")
SETUP = [{"name": "Build", "status": "completed", "conclusion": "action_required", "url": "u"}]
GREEN = [{"name": "Build", "status": "completed", "conclusion": "success"}]


def _facts(**kwargs: Any) -> Facts:
    defaults: dict[str, Any] = {
        "upstream": "apache/spark",
        "sha": "head1",
        "branch": "SPARK-1-fix",
        "base_branch": "master",
        "fork": "newbie/spark",
        "setup_checks": [SetupCheck(name="Build")],
        "fork_read": True,
        "workflows": [{"path": SPARK_BUILD_WORKFLOW, "state": "active"}],
    }
    defaults.update(kwargs)
    return Facts(**defaults)


def _kinds(facts: Facts, workflow: str = SPARK_BUILD_WORKFLOW, spark: bool = True) -> list[str]:
    return [p.kind for p in diagnose(facts, build_workflow=workflow, spark=spark)]


def _run(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "id": 7,
        "path": SPARK_BUILD_WORKFLOW,
        "branch": "SPARK-1-fix",
        "status": "completed",
        "conclusion": "success",
        "url": "https://github.com/newbie/spark/actions/runs/7",
    }
    defaults.update(kwargs)
    return defaults


class TestDiagnose:
    """The decision list, one fork state at a time."""

    def test_actions_never_enabled_on_the_fork(self) -> None:
        facts = _facts(workflows=[{"path": SPARK_BUILD_WORKFLOW, "state": "disabled_fork"}])
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "actions-not-enabled"
        assert problem.who == "author"
        assert "https://github.com/newbie/spark/actions" in problem.remediation
        assert "I understand my workflows, go ahead and enable them" in problem.remediation
        assert "`SPARK-1-fix`" in problem.remediation

    def test_workflow_listing_refused_means_actions_are_probably_off(self) -> None:
        facts = _facts(workflows=None)
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "actions-unreadable"
        assert "https://github.com/newbie/spark/settings/actions" in problem.remediation
        assert "Allow all actions and reusable workflows" in problem.remediation

    @pytest.mark.parametrize("state", ["disabled_manually", "disabled_inactivity"])
    def test_build_workflow_disabled(self, state: str) -> None:
        facts = _facts(workflows=[{"path": SPARK_BUILD_WORKFLOW, "state": state}])
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "workflow-disabled"
        assert "actions/workflows/build_main.yml" in problem.remediation
        assert "Enable workflow" in problem.remediation

    def test_pr_from_fork_master_on_spark_wins_over_everything_on_the_fork(self) -> None:
        facts = _facts(branch="master", jira="SPARK-42", fork_read=False, workflows=None)
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "from-fork-master"
        assert "git checkout -b SPARK-42" in problem.remediation
        assert "Sync fork" in problem.remediation

    def test_master_is_only_special_under_the_spark_profile(self) -> None:
        facts = _facts(branch="master", fork_read=False, workflows=None)
        assert _kinds(facts, spark=False) == ["no-run"]

    def test_deleted_fork(self) -> None:
        facts = _facts(fork="", fork_deleted=True, fork_read=False, workflows=None)
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "fork-deleted"
        assert "Fork `apache/spark` again" in problem.remediation

    def test_branch_without_the_workflow_file_needs_a_rebase(self) -> None:
        facts = _facts(workflow_at_head=False, base_branch="branch-3.5")
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "branch-too-old"
        assert "git rebase upstream/branch-3.5" in problem.remediation
        assert "--force-with-lease origin SPARK-1-fix" in problem.remediation

    def test_skipped_build(self) -> None:
        facts = _facts(runs=[_run(conclusion="skipped")])
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "build-skipped"
        assert "actions/runs/7" in problem.summary
        assert "[skip ci]" in problem.summary

    def test_startup_failure_points_at_actions_permissions(self) -> None:
        facts = _facts(runs=[_run(conclusion="startup_failure")])
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "startup-failure"
        assert "Allow all actions and reusable workflows" in problem.remediation

    def test_a_build_that_exists_is_nobodys_problem(self) -> None:
        facts = _facts(runs=[_run(status="in_progress", conclusion="")])
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "run-not-linked"
        assert problem.who == "none"

    def test_newest_build_on_the_pr_branch_is_the_one_read(self) -> None:
        facts = _facts(
            runs=[
                _run(id=5, conclusion="skipped"),
                _run(id=9, conclusion="success"),
                _run(id=12, conclusion="skipped", branch="master"),
            ]
        )
        assert _kinds(facts) == ["run-not-linked"]

    def test_no_run_and_nothing_else_known(self) -> None:
        facts = _facts(workflow_at_head=True)
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "no-run"
        assert "git commit --allow-empty" in problem.remediation

    def test_without_a_known_workflow_the_checks_own_words_are_the_diagnosis(self) -> None:
        check = SetupCheck(
            name="CLA", url="https://cla.example/1", title="Sign the CLA", summary="Go sign it"
        )
        facts = _facts(setup_checks=[check], fork_read=False, workflows=None)
        [problem] = diagnose(facts, build_workflow="", spark=False)
        assert problem.kind == "check-needs-action"
        assert "`CLA`" in problem.summary and "*Sign the CLA*" in problem.summary
        assert "https://cla.example/1" in problem.remediation
        assert problem.evidence == "Go sign it"

    def test_a_branch_on_the_upstream_repo_has_no_fork_to_fix(self) -> None:
        facts = _facts(fork="apache/spark")
        assert _kinds(facts) == ["check-needs-action"]

    def test_runs_awaiting_approval_are_the_maintainers(self) -> None:
        facts = _facts(
            setup_checks=[],
            awaiting_runs=[
                {"id": 1, "name": "Labeler", "path": ".github/workflows/x.yml", "on_base": False},
                {"id": 2, "name": "Lint", "path": ".github/workflows/y.yml", "on_base": True},
            ],
        )
        [problem] = diagnose(facts, build_workflow=SPARK_BUILD_WORKFLOW, spark=True)
        assert problem.kind == "awaiting-approval"
        assert problem.who == "maintainer"
        assert "`Labeler`, `Lint`" in problem.summary
        assert "`.github/workflows/x.yml` is not on `master`" in problem.summary
        assert "y.yml` is" not in problem.summary

    def test_both_sides_maintainer_first(self) -> None:
        facts = _facts(
            awaiting_runs=[{"id": 1, "name": "CI", "path": "p", "on_base": True}],
            workflows=[{"path": SPARK_BUILD_WORKFLOW, "state": "disabled_fork"}],
        )
        assert _kinds(facts) == ["awaiting-approval", "actions-not-enabled"]

    def test_nothing_wrong(self) -> None:
        assert _kinds(_facts(setup_checks=[])) == []


class TestCompose:
    CFG = ForkSetupConfig()

    def test_only_the_authors_problems_go_in(self) -> None:
        problems = [
            Problem(kind="awaiting-approval", who="maintainer", summary="approve me"),
            Problem(kind="actions-not-enabled", who="author", summary="S", remediation="R"),
        ]
        body = compose_comment(
            problems, author="newbie", head_sha="abcdef123", base_branch="master", cfg=self.CFG
        )
        assert body.startswith("Hi @newbie")
        assert "`abcdef1`" in body
        assert "approve me" not in body
        assert "**GitHub Actions not enabled on the fork**" in body
        assert "**What to do:** R" in body

    def test_several_are_numbered_and_evidence_quoted(self) -> None:
        problems = [
            Problem(kind="no-run", who="author", summary="one"),
            Problem(kind="check-needs-action", who="author", summary="two", evidence="a\nb"),
        ]
        body = compose_comment(
            problems, author="newbie", head_sha="abc", base_branch="", cfg=self.CFG
        )
        assert "**1. No build in the fork" in body and "**2. A check needs action**" in body
        assert "> a\n> b" in body

    def test_nothing_for_the_author_means_no_draft(self) -> None:
        problems = [Problem(kind="run-not-linked", who="none", summary="wait")]
        assert (
            compose_comment(problems, author="a", head_sha="s", base_branch="", cfg=self.CFG) == ""
        )

    def test_other_handles_are_not_pinged(self) -> None:
        problems = [Problem(kind="no-run", who="author", summary="ask @someone-else")]
        body = compose_comment(
            problems, author="newbie", head_sha="s", base_branch="", cfg=self.CFG
        )
        assert "@newbie" in body
        assert "`@someone-else`" in body


class TestHelpers:
    def test_problems_round_trip_and_unknown_values_fall_back(self) -> None:
        problem = Problem(kind="no-run", who="author", summary="s", remediation="r", url="u")
        assert problems_from([problem.to_dict()]) == [problem]
        [odd] = problems_from([{"kind": "nope", "who": "everyone"}, "junk"])
        assert (odd.kind, odd.who) == ("check-needs-action", "author")
        assert problems_from(None) == []

    def test_build_workflow_comes_from_config_then_the_spark_profile(self) -> None:
        assert build_workflow_for(SPARK) == SPARK_BUILD_WORKFLOW
        assert build_workflow_for(GENERIC) == ""
        custom = ProjectConfig(
            owner="acme", repo="widget", fork_setup=ForkSetupConfig(build_workflow="ci.yml")
        )
        assert build_workflow_for(custom) == "ci.yml"

    def test_max_per_run_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            ForkSetupConfig(max_per_run=0)

    @pytest.mark.django_db
    def test_touched_workflows(self) -> None:
        pr = PullRequestFactory(
            changed_files=[
                {"filename": ".github/workflows/build.yml"},
                "src/main.scala",
                ".github/workflows/build.yml",
            ]
        )
        assert touched_workflows(pr) == [".github/workflows/build.yml"]


# --------------------------------------------------------------------------
# Against the database, with a fake forge
# --------------------------------------------------------------------------


@pytest.fixture
def project(db: Any) -> Project:
    return ProjectFactory(owner="apache", repo="spark")


def _setup_pr(project: Project, **kwargs: Any) -> PullRequest:
    defaults: dict[str, Any] = {
        "project": project,
        "author": "newbie",
        "head_sha": "head1",
        "head_branch": "SPARK-1-fix",
        "base_branch": "master",
        "ci_checks_sha": "head1",
        "ci_status": "fail",
        "ci_checks": SETUP,
        "github_updated_at": timezone.now(),
    }
    defaults.update(kwargs)
    return PullRequestFactory(**defaults)


def _fresh(pr: PullRequest) -> ForkSetupAssessment:
    """A row the sweep has just found: pinned to the head, nothing read yet."""
    return ForkSetupAssessmentFactory(
        pull_request=pr,
        status="unchecked",
        problems=[],
        kinds=[],
        needs_author=False,
        covers_ci_failure=False,
        fork_full_name="",
        comment_body="",
    )


def _forge(**kwargs: Any) -> MagicMock:
    """A forge whose PR comes from newbie/spark, with Actions never enabled there."""
    forge = MagicMock()
    forge.get_pull_request.return_value = kwargs.get(
        "detail",
        {
            "state": "open",
            "head": {"sha": "head1", "ref": "SPARK-1-fix", "repo": {"full_name": "newbie/spark"}},
            "base": {"ref": "master"},
        },
    )
    forge.get_commit_checks.return_value = kwargs.get("checks", SETUP)
    forge.list_runs_awaiting_approval.return_value = kwargs.get("awaiting", [])
    forge.list_repo_workflows.return_value = kwargs.get(
        "workflows", [{"path": SPARK_BUILD_WORKFLOW, "state": "disabled_fork"}]
    )
    forge.list_workflow_runs_for_commit.return_value = kwargs.get("runs", [])
    forge.file_exists.return_value = kwargs.get("file_exists", True)
    forge.create_issue_comment.return_value = {"id": 99}
    return forge


@pytest.mark.django_db
class TestCheck:
    def test_diagnoses_and_drafts_from_the_fork(self, project: Project) -> None:
        row = _fresh(_setup_pr(project))
        forge = _forge()
        outcome = fork_setup.check(row, SPARK, forge)
        row.refresh_from_db()
        assert "actions-not-enabled" in outcome
        assert row.status == "diagnosed"
        assert row.needs_author and not row.needs_maintainer
        assert row.kinds == ["actions-not-enabled"]
        assert row.fork_full_name == "newbie/spark"
        assert "Hi @newbie" in row.comment_body
        assert "https://github.com/newbie/spark/actions" in row.comment_body
        assert row.checked_at is not None and row.attempts == 1
        forge.list_repo_workflows.assert_called_once_with("newbie", "spark")
        forge.list_runs_awaiting_approval.assert_called_once_with(
            "apache", "spark", head_sha="head1"
        )

    def test_another_failing_check_keeps_the_pr_in_ci_failures(self, project: Project) -> None:
        checks = [*SETUP, {"name": "Tests", "status": "completed", "conclusion": "failure"}]
        row = _fresh(_setup_pr(project, ci_checks=checks))
        fork_setup.check(row, SPARK, _forge(checks=checks))
        row.refresh_from_db()
        assert row.needs_author
        assert not row.covers_ci_failure
        # Setup alone covers it.
        alone = _fresh(_setup_pr(project, number=2))
        fork_setup.check(alone, SPARK, _forge())
        alone.refresh_from_db()
        assert alone.covers_ci_failure

    def test_runs_awaiting_approval_are_checked_against_the_base_branch(
        self, project: Project
    ) -> None:
        row = _fresh(_setup_pr(project, ci_checks=GREEN, ci_status="pass"))
        forge = _forge(
            checks=GREEN,
            awaiting=[
                {"id": 31, "name": "Sneaky", "path": ".github/workflows/s.yml", "head_sha": "head1"}
            ],
            file_exists=False,
        )
        fork_setup.check(row, SPARK, forge)
        row.refresh_from_db()
        assert row.needs_maintainer and not row.needs_author
        assert row.comment_body == ""
        assert row.awaiting_runs[0]["on_base"] is False
        forge.file_exists.assert_called_once_with(
            "apache", "spark", ".github/workflows/s.yml", "master"
        )
        # No setup check, so the fork is never read.
        forge.list_repo_workflows.assert_not_called()

    def test_generic_project_quotes_the_check(self, db: Any) -> None:
        project = ProjectFactory(owner="acme", repo="widget")
        row = _fresh(_setup_pr(project))
        forge = _forge()
        forge.get_commit_check_failures.return_value = [
            {"check": "Build", "conclusion": "action_required", "title": "Do X", "summary": "how"}
        ]
        fork_setup.check(row, GENERIC, forge)
        row.refresh_from_db()
        assert row.kinds == ["check-needs-action"]
        assert "*Do X*" in row.comment_body
        forge.list_repo_workflows.assert_not_called()

    def test_closed_pr_is_resolved(self, project: Project) -> None:
        row = _fresh(_setup_pr(project))
        fork_setup.check(row, SPARK, _forge(detail={"state": "closed"}))
        row.refresh_from_db()
        assert row.status == "resolved"

    def test_a_head_the_poller_has_not_seen_waits(self, project: Project) -> None:
        row = _fresh(_setup_pr(project))
        detail = {"state": "open", "head": {"sha": "head2", "repo": {"full_name": "n/s"}}}
        fork_setup.check(row, SPARK, _forge(detail=detail))
        row.refresh_from_db()
        assert row.status == "unchecked"
        assert "head1 -> head2" in row.last_error

    def test_unreadable_pr_is_an_attempt_with_a_reason(self, project: Project) -> None:
        row = _fresh(_setup_pr(project))
        forge = _forge()
        forge.get_pull_request.side_effect = RuntimeError("boom")
        fork_setup.check(row, SPARK, forge)
        row.refresh_from_db()
        assert row.status == "unchecked" and row.attempts == 1
        assert "boom" in row.last_error

    def test_best_effort_reads_degrade_with_a_note(self, project: Project) -> None:
        row = _fresh(_setup_pr(project))
        forge = _forge()
        forge.get_commit_checks.side_effect = RuntimeError("checks down")
        forge.list_runs_awaiting_approval.side_effect = RuntimeError("runs down")
        forge.list_repo_workflows.side_effect = RuntimeError("fork down")
        fork_setup.check(row, SPARK, forge)
        row.refresh_from_db()
        # Stored checks still say "setup"; the fork could not be read.
        assert row.kinds == ["no-run"]
        assert "could not re-read the checks" in row.last_error
        assert "could not list runs awaiting approval" in row.last_error
        assert "could not read the fork newbie/spark" in row.last_error

    def test_branch_without_the_workflow(self, project: Project) -> None:
        row = _fresh(_setup_pr(project))
        forge = _forge(
            workflows=[{"path": SPARK_BUILD_WORKFLOW, "state": "active"}], file_exists=False
        )
        fork_setup.check(row, SPARK, forge)
        row.refresh_from_db()
        assert row.kinds == ["branch-too-old"]
        forge.file_exists.assert_called_once_with("newbie", "spark", SPARK_BUILD_WORKFLOW, "head1")

    def test_deleted_fork(self, project: Project) -> None:
        row = _fresh(_setup_pr(project))
        detail = {"state": "open", "head": {"sha": "head1", "ref": "x", "repo": None}}
        fork_setup.check(row, SPARK, _forge(detail=detail))
        row.refresh_from_db()
        assert row.kinds == ["fork-deleted"]

    def test_a_moved_head_resets_the_row(self, project: Project) -> None:
        pr = _setup_pr(project, head_sha="head2", ci_checks_sha="head2")
        row = ForkSetupAssessmentFactory(pull_request=pr, head_sha="head1", disposition="dismissed")
        detail = {
            "state": "open",
            "head": {"sha": "head2", "ref": "SPARK-1-fix", "repo": {"full_name": "newbie/spark"}},
        }
        fork_setup.check(row, SPARK, _forge(detail=detail))
        row.refresh_from_db()
        assert row.head_sha == "head2"
        assert row.disposition == "pending"
        assert row.status == "diagnosed"


@pytest.mark.django_db
class TestDiagnosePR:
    def test_closed(self, project: Project) -> None:
        pr = _setup_pr(project, state="closed")
        assert "nothing to diagnose" in fork_setup.diagnose_pr(pr, MagicMock())

    def test_no_client(self, project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
        pr = _setup_pr(project)
        monkeypatch.setattr(fork_setup, "_project_config_for", lambda pr: SPARK)
        monkeypatch.setattr(fork_setup, "_client_for", lambda pc, oc: None)
        assert "no forge client" in fork_setup.diagnose_pr(pr, MagicMock())
        assert ForkSetupAssessment.objects.get(pull_request=pr).attempts == 1

    def test_runs_the_check_and_closes_the_client(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pr = _setup_pr(project)
        forge = _forge()
        monkeypatch.setattr(fork_setup, "_project_config_for", lambda pr: SPARK)
        monkeypatch.setattr(fork_setup, "_client_for", lambda pc, oc: forge)
        assert "actions-not-enabled" in fork_setup.diagnose_pr(pr, MagicMock())
        forge.close.assert_called_once()


@pytest.mark.django_db
class TestScanProject:
    def test_finds_both_kinds_and_diagnoses_them(self, project: Project) -> None:
        setup_pr = _setup_pr(project, number=1)
        waiting_pr = _setup_pr(
            project, number=2, head_sha="w1", ci_checks_sha="w1", ci_checks=GREEN, ci_status="pass"
        )
        _setup_pr(
            project, number=3, head_sha="g1", ci_checks_sha="g1", ci_checks=GREEN, ci_status="pass"
        )
        forge = _forge()
        forge.list_runs_awaiting_approval.return_value = [
            {"id": 5, "name": "CI", "path": "p", "head_sha": "w1"}
        ]

        def detail(owner: str, repo: str, number: int) -> dict[str, Any]:
            sha = {1: "head1", 2: "w1"}[number]
            return {
                "state": "open",
                "head": {"sha": sha, "ref": "b", "repo": {"full_name": "newbie/spark"}},
                "base": {"ref": "master"},
            }

        forge.get_pull_request.side_effect = detail
        forge.get_commit_checks.side_effect = lambda o, r, sha: SETUP if sha == "head1" else GREEN
        result = scan_project(project, SPARK, forge)
        assert (result.candidates, result.awaiting, result.diagnosed) == (2, 1, 2)
        assert (result.needs_author, result.needs_maintainer) == (1, 1)
        assert ForkSetupAssessment.objects.get(pull_request=setup_pr).needs_author
        assert ForkSetupAssessment.objects.get(pull_request=waiting_pr).needs_maintainer
        # The project-wide listing is reused; no per-PR listing.
        forge.list_runs_awaiting_approval.assert_called_once_with("apache", "spark")
        assert "2 candidate PR(s)" in result.summary()

    def test_budget_leaves_the_rest_for_next_time(self, project: Project) -> None:
        for n in range(3):
            _setup_pr(project, number=10 + n)
        cfg = ProjectConfig(owner="apache", repo="spark", fork_setup=ForkSetupConfig(max_per_run=1))
        result = scan_project(project, cfg, _forge())
        assert (result.diagnosed, result.waiting) == (1, 2)

    def test_already_diagnosed_rows_are_not_re_read(self, project: Project) -> None:
        ForkSetupAssessmentFactory(pull_request=_setup_pr(project))
        forge = _forge()
        result = scan_project(project, SPARK, forge)
        assert result.diagnosed == 0
        forge.get_pull_request.assert_not_called()

    def test_a_run_that_starts_waiting_later_is_picked_up(self, project: Project) -> None:
        row = ForkSetupAssessmentFactory(pull_request=_setup_pr(project))
        forge = _forge(awaiting=[{"id": 8, "name": "CI", "path": "p", "head_sha": "head1"}])
        result = scan_project(project, SPARK, forge)
        row.refresh_from_db()
        assert result.diagnosed == 1
        assert row.needs_author and row.needs_maintainer
        assert row.kinds == ["awaiting-approval", "actions-not-enabled"]

    def test_an_approval_given_elsewhere_while_the_author_side_stays(
        self, project: Project
    ) -> None:
        row = ForkSetupAssessmentFactory(
            pull_request=_setup_pr(project),
            needs_maintainer=True,
            kinds=["awaiting-approval", "actions-not-enabled"],
            disposition="posted",
        )
        scan_project(project, SPARK, _forge())
        row.refresh_from_db()
        assert not row.needs_maintainer and row.needs_author
        assert row.kinds == ["actions-not-enabled"]
        # Re-reading does not take back what the operator already did.
        assert row.disposition == "posted"

    def test_an_unlisted_sweep_has_no_opinion_on_approvals(self, project: Project) -> None:
        ForkSetupAssessmentFactory(pull_request=_setup_pr(project), needs_maintainer=True)
        forge = _forge()
        forge.list_runs_awaiting_approval.side_effect = RuntimeError("down")
        assert scan_project(project, SPARK, forge).diagnosed == 0

    def test_an_approval_given_on_github_clears_the_row(self, project: Project) -> None:
        pr = _setup_pr(project, ci_checks=GREEN, ci_status="pass")
        row = ForkSetupAssessmentFactory(
            pull_request=pr, needs_author=False, needs_maintainer=True, kinds=["awaiting-approval"]
        )
        result = scan_project(project, SPARK, _forge())
        row.refresh_from_db()
        assert result.cleared == 1
        assert not row.needs_maintainer and row.status == "resolved"

    def test_a_setup_check_that_went_away_clears_the_author_half(self, project: Project) -> None:
        pr = _setup_pr(project, ci_checks=GREEN, ci_status="pass")
        row = ForkSetupAssessmentFactory(pull_request=pr)
        scan_project(project, SPARK, _forge())
        row.refresh_from_db()
        assert not row.needs_author and row.status == "resolved"

    def test_listing_failure_keeps_approvals_and_says_so(self, project: Project) -> None:
        pr = _setup_pr(project, ci_checks=GREEN, ci_status="pass")
        row = ForkSetupAssessmentFactory(pull_request=pr, needs_author=False, needs_maintainer=True)
        forge = _forge()
        forge.list_runs_awaiting_approval.side_effect = RuntimeError("nope")
        result = scan_project(project, SPARK, forge)
        row.refresh_from_db()
        assert row.needs_maintainer
        assert "could not list runs awaiting approval" in result.note


@pytest.mark.django_db
class TestScan:
    def test_only_enabled_projects_unless_every_project(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_pr(project)
        off = ProjectConfig(owner="apache", repo="spark", fork_setup=ForkSetupConfig(enabled=False))
        monkeypatch.setattr(fork_setup, "_client_for", lambda pc, oc: _forge())
        assert fork_setup.scan([off], MagicMock()) == []
        [result] = fork_setup.scan([off, "junk"], MagicMock(), every_project=True)
        assert result.diagnosed == 1

    def test_unknown_project_or_client_is_skipped(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fork_setup, "_client_for", lambda pc, oc: None)
        assert fork_setup.scan([SPARK], MagicMock()) == []
        ProjectFactory(owner="apache", repo="spark")
        assert fork_setup.scan([SPARK], MagicMock()) == []


@pytest.mark.django_db
class TestOperatorActions:
    def test_posted(self, project: Project) -> None:
        row = ForkSetupAssessmentFactory(pull_request=_setup_pr(project))
        record_posted(row, "edited text", 12)
        row.refresh_from_db()
        assert row.disposition == "posted" and row.edited_body == "edited text"
        assert row.forge_comment_id == 12 and row.post_count == 1
        assert OperatorAction.objects.filter(action_type="post_fork_setup_comment").exists()

    def test_dismiss_and_restore(self, project: Project) -> None:
        row = ForkSetupAssessmentFactory(pull_request=_setup_pr(project))
        record_dismissal(row, "wrong", "Actions are on")
        row.refresh_from_db()
        assert (row.disposition, row.dismiss_reason) == ("dismissed", "wrong")
        restore(row)
        row.refresh_from_db()
        assert row.disposition == "pending" and row.dismiss_reason == ""
        assert OperatorAction.objects.filter(action_type="restore_fork_setup").exists()

    def test_post_blocker(self, project: Project) -> None:
        row = ForkSetupAssessmentFactory(pull_request=_setup_pr(project))
        assert post_blocker(_forge(), row) == ""
        assert "no longer open" in post_blocker(_forge(detail={"state": "closed"}), row)
        assert "new commits" in post_blocker(
            _forge(detail={"state": "open", "head": {"sha": "head9"}}), row
        )
        broken = _forge()
        broken.get_pull_request.side_effect = RuntimeError
        assert "could not read" in post_blocker(broken, row)

    def test_post_blocker_notices_the_build_was_linked(self, project: Project) -> None:
        row = ForkSetupAssessmentFactory(pull_request=_setup_pr(project))
        assert "no check on this commit needs setup" in post_blocker(_forge(checks=GREEN), row)
        row.refresh_from_db()
        assert row.status == "resolved" and not row.needs_author

    def test_approve_lists_live_and_approves_what_is_waiting(self, project: Project) -> None:
        row = ForkSetupAssessmentFactory(
            pull_request=_setup_pr(project),
            needs_author=False,
            needs_maintainer=True,
            kinds=["awaiting-approval"],
            problems=[{"kind": "awaiting-approval", "who": "maintainer", "summary": "s"}],
            awaiting_runs=[{"id": 5}, {"id": 6}],
        )
        forge = _forge(awaiting=[{"id": 6, "name": "CI", "head_sha": "head1"}])
        result = approve(forge, row)
        row.refresh_from_db()
        forge.approve_workflow_run.assert_called_once_with("apache", "spark", 6)
        assert result.approved == [6]
        assert "Approved 1 workflow run(s)" in result.message()
        assert not row.needs_maintainer and row.status == "resolved"
        assert row.problems == [] and row.approved_run_ids == [6]
        assert OperatorAction.objects.filter(action_type="approve_fork_workflows").exists()

    def test_approve_without_permission_keeps_the_row(self, project: Project) -> None:
        row = ForkSetupAssessmentFactory(
            pull_request=_setup_pr(project), needs_maintainer=True, awaiting_runs=[{"id": 6}]
        )
        forge = _forge(awaiting=[{"id": 6, "name": "CI", "head_sha": "head1"}])
        response = httpx.Response(403, request=httpx.Request("POST", "https://x"))
        forge.approve_workflow_run.side_effect = httpx.HTTPStatusError(
            "403", request=response.request, response=response
        )
        result = approve(forge, row)
        row.refresh_from_db()
        assert "lacks write access" in result.message()
        assert row.needs_maintainer
        assert row.approved_at is None

    def test_approve_blocked_by_a_push(self, project: Project) -> None:
        row = ForkSetupAssessmentFactory(pull_request=_setup_pr(project), needs_maintainer=True)
        forge = _forge(detail={"state": "open", "head": {"sha": "head9"}})
        result = approve(forge, row)
        assert result.message().startswith("Not approved: the PR has new commits")
        forge.approve_workflow_run.assert_not_called()

    def test_nothing_waiting_any_more(self, project: Project) -> None:
        row = ForkSetupAssessmentFactory(
            pull_request=_setup_pr(project), needs_author=False, needs_maintainer=True
        )
        result = approve(_forge(awaiting=[]), row)
        row.refresh_from_db()
        assert "Nothing is waiting" in result.message()
        assert row.status == "resolved"


@pytest.mark.django_db
class TestQueueFilters:
    def test_tab_membership(self, project: Project) -> None:
        listed = ForkSetupAssessmentFactory(pull_request=_setup_pr(project, number=1))
        approval = ForkSetupAssessmentFactory(
            pull_request=_setup_pr(project, number=2),
            needs_author=False,
            needs_maintainer=True,
            disposition="posted",
        )
        ForkSetupAssessmentFactory(pull_request=_setup_pr(project, number=3), disposition="posted")
        ForkSetupAssessmentFactory(pull_request=_setup_pr(project, number=4), status="unchecked")
        ForkSetupAssessmentFactory(pull_request=_setup_pr(project, number=5), head_sha="old")
        ForkSetupAssessmentFactory(pull_request=_setup_pr(project, number=6, state="closed"))
        members = set(
            PullRequest.objects.filter(ForkSetupAssessment.queue_q()).values_list("pk", flat=True)
        )
        assert members == {listed.pull_request_id, approval.pull_request_id}

    def test_ci_failures_tab_leaves_out_setup_problems(self, project: Project) -> None:
        covered = _setup_pr(project, number=1)
        CIFailureAssessmentFactory(pull_request=covered)
        ForkSetupAssessmentFactory(pull_request=covered)
        stale = _setup_pr(project, number=2)
        CIFailureAssessmentFactory(pull_request=stale)
        ForkSetupAssessmentFactory(pull_request=stale, head_sha="older")
        plain = CIFailureAssessmentFactory(pull_request=_setup_pr(project, number=3))
        mixed = _setup_pr(project, number=4)
        CIFailureAssessmentFactory(pull_request=mixed)
        ForkSetupAssessmentFactory(pull_request=mixed, covers_ci_failure=False)
        members = set(
            PullRequest.objects.filter(CIFailureAssessment.queue_q()).values_list("pk", flat=True)
        )
        assert members == {stale.pk, plain.pull_request_id, mixed.pk}
        candidates = {pr.pk for pr in ci_failure.candidate_prs(project, SPARK)}
        assert covered.pk not in candidates and stale.pk in candidates
