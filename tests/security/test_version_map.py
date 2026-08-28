"""Tests for cheap version mapping: cited files vs every shipping branch."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.core.models import SecurityVerification
from franktheunicorn.review.tool_executor import ExecResult
from franktheunicorn.security.verifier import select_branches
from franktheunicorn.security.version_map import (
    VERSION_MAP_AGENT,
    extract_report_paths,
    map_report_versions,
    release_line_from_branch,
)
from tests.factories import ProjectFactory, SecurityReportFactory
from tests.security.test_verifier import (
    _BRANCH_LISTING,
    _FakeExecutor,
    _operator,
    _verifier,
)


def test_release_line_from_branch() -> None:
    assert release_line_from_branch("branch-3.5") == "3.5.x"
    assert release_line_from_branch("branch-4.0") == "4.0.x"
    assert release_line_from_branch("release-3.5") == "3.5.x"
    assert release_line_from_branch("master") == "unreleased"
    assert release_line_from_branch("main") == "unreleased"
    assert release_line_from_branch("topic/foo") == "topic/foo"


def test_extract_paths_from_report_text() -> None:
    report = SecurityReportFactory.build(
        raw_text="Hole in sql/core/src/main/scala/org/apache/spark/sql/Foo.scala:12",
        proposed_patch="",
        parsed_component="",
        title="",
        parsed_poc="",
    )
    assert extract_report_paths(report) == [
        "sql/core/src/main/scala/org/apache/spark/sql/Foo.scala"
    ]


def test_extract_paths_prefers_patch_and_keeps_casing() -> None:
    report = SecurityReportFactory.build(
        raw_text="see Foo.scala",
        proposed_patch="--- a/sql/core/Foo.scala\n+++ b/sql/core/Foo.scala\n",
        parsed_component="",
        title="",
        parsed_poc="",
    )
    assert extract_report_paths(report) == ["sql/core/Foo.scala"]


@pytest.mark.django_db
def test_extract_paths_uses_existing_verification_evidence() -> None:
    report = SecurityReportFactory(
        raw_text="no paths here",
        proposed_patch="",
        parsed_component="",
        title="vague",
    )
    SecurityVerification.objects.create(
        report=report,
        branch="master",
        verdict="affected",
        evidence="core/src/main/scala/org/apache/spark/Util.scala:40 — missing check",
        agent="claude/sonnet",
    )
    assert extract_report_paths(report) == ["core/src/main/scala/org/apache/spark/Util.scala"]


class _PathExecutor(_FakeExecutor):
    """ls-tree and apply --check differ per branch; the parent matcher keys on
    the first four tokens, which collide across branches."""

    def __init__(
        self,
        trees: dict[str, str],
        apply_ok: dict[str, bool] | None = None,
        cwd: str = "/w/spark",
    ) -> None:
        super().__init__(
            {
                "for-each-ref": _BRANCH_LISTING,
                "symbolic-ref": ExecResult(returncode=0, stdout="origin/master\n", stderr=""),
                "fetch": ExecResult(returncode=0, stdout="", stderr=""),
            },
            cwd=cwd,
        )
        self.trees = trees
        self.apply_ok = apply_ok or {}
        self.current_branch = ""
        self.apply_stdin: list[str] = []

    def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        self.calls.append(cmd)
        if cmd[:4] == ["git", "checkout", "--detach", "--force"] and len(cmd) >= 5:
            self.current_branch = cmd[4].removeprefix("origin/")
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return ExecResult(returncode=0, stdout="abc123\n", stderr="")
        if cmd[:3] == ["git", "apply", "--check"]:
            self.apply_stdin.append(stdin or "")
            assert cmd[-1] == "-"  # explicit stdin; a missing dash is "no patch"
            ok = self.apply_ok.get(self.current_branch, False)
            return ExecResult(
                returncode=0 if ok else 1,
                stdout="",
                stderr="" if ok else "patch does not apply",
            )
        if cmd[:4] == ["git", "ls-tree", "-r", "--name-only"] and len(cmd) >= 5:
            branch = cmd[4].removeprefix("origin/")
            files = self.trees.get(branch, "")
            return ExecResult(returncode=0, stdout=files, stderr="")
        return super().run(cmd, cwd, timeout, stdin)


@pytest.mark.django_db
class TestMapReportVersions:
    def _report(self) -> Any:
        return SecurityReportFactory(
            project=ProjectFactory(owner="apache", repo="spark"),
            raw_text="Bug in sql/core/src/Foo.scala via unsafe deserialize",
        )

    def test_writes_a_row_per_shipping_branch(self) -> None:
        report = self._report()
        executor = _PathExecutor(
            {
                "master": "sql/core/src/Foo.scala\n",
                "branch-4.0": "sql/core/src/Foo.scala\n",
                "branch-3.5": "sql/core/src/Bar.scala\n",
            }
        )
        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            run = map_report_versions(report, _operator())

        assert run.error == ""
        by_branch = {r.branch: r for r in run.results}
        assert by_branch["master"].verdict == "affected"
        assert by_branch["branch-4.0"].verdict == "affected"
        assert by_branch["branch-3.5"].verdict == "not-affected"
        assert by_branch["master"].version_impact[0]["name"] == "unreleased"
        assert by_branch["branch-3.5"].version_impact[0]["name"] == "3.5.x"
        rows = list(SecurityVerification.objects.filter(report=report))
        assert {r.branch for r in rows} == {"master", "branch-4.0", "branch-3.5"}
        assert all(r.agent == VERSION_MAP_AGENT for r in rows)

    def test_does_not_overwrite_a_deep_verifier_row(self) -> None:
        report = self._report()
        kept = SecurityVerification.objects.create(
            report=report,
            branch="master",
            verdict="affected",
            summary="I read the code.",
            evidence="sql/core/src/Foo.scala:10",
            agent="claude/sonnet",
            version_impact=[],
        )
        executor = _PathExecutor({"master": "sql/core/src/Foo.scala\n", "branch-4.0": ""})
        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            map_report_versions(report, _operator())

        kept.refresh_from_db()
        assert kept.verdict == "affected"
        assert kept.summary == "I read the code."
        assert kept.agent == "claude/sonnet"
        assert kept.version_impact == [
            {
                "name": "unreleased",
                "status": "affected",
                "reason": "cited file(s) still present: sql/core/src/Foo.scala",
            }
        ]
        extra = SecurityVerification.objects.get(report=report, branch="branch-4.0")
        assert extra.agent == VERSION_MAP_AGENT

    def test_records_which_branches_the_proposed_patch_applies_to(self) -> None:
        report = self._report()
        report.proposed_patch = (
            "--- a/sql/core/src/Foo.scala\n+++ b/sql/core/src/Foo.scala\n@@ -1 +1 @@\n-old\n+new\n"
        )
        report.save(update_fields=["proposed_patch"])
        executor = _PathExecutor(
            {
                "master": "sql/core/src/Foo.scala\n",
                "branch-4.0": "sql/core/src/Foo.scala\n",
                "branch-3.5": "sql/core/src/Foo.scala\n",
            },
            apply_ok={"master": True, "branch-4.0": True, "branch-3.5": False},
        )
        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            run = map_report_versions(report, _operator())

        by_branch = {r.branch: r for r in run.results}
        assert "applies cleanly" in by_branch["master"].summary
        assert "applies cleanly" in by_branch["branch-4.0"].summary
        assert "does not apply" in by_branch["branch-3.5"].summary
        assert by_branch["master"].confidence == 0.7
        assert "proposed patch applies cleanly" in by_branch["master"].version_impact[0]["reason"]
        assert (
            "proposed patch does not apply" in by_branch["branch-3.5"].version_impact[0]["reason"]
        )
        apply_calls = [c for c in executor.calls if c[:3] == ["git", "apply", "--check"]]
        assert len(apply_calls) == 3
        assert all(payload == report.proposed_patch for payload in executor.apply_stdin)

    def test_no_patch_does_not_try_git_apply(self) -> None:
        report = self._report()
        executor = _PathExecutor({"master": "sql/core/src/Foo.scala\n"})
        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            map_report_versions(report, _operator())

        assert not any(c[:3] == ["git", "apply", "--check"] for c in executor.calls)

    def test_a_patch_without_cited_paths_is_enough_to_run(self) -> None:
        """A scanner patch that names no source files still has something git can try."""
        report = SecurityReportFactory(
            project=ProjectFactory(owner="apache", repo="spark"),
            raw_text="someone said it was bad",
            title="hmm",
            proposed_patch="diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n",
        )
        executor = _PathExecutor({}, apply_ok={"master": True, "branch-4.0": False})
        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            run = map_report_versions(report, _operator())

        assert run.error == ""
        by_branch = {r.branch: r for r in run.results}
        assert by_branch["master"].verdict == "affected"
        assert "applies cleanly" in by_branch["master"].summary
        assert by_branch["branch-4.0"].verdict == "not-affected"
        assert "does not apply" in by_branch["branch-4.0"].summary
        assert not any(c[:2] == ["git", "ls-tree"] for c in executor.calls)

    def test_a_failed_checkout_on_a_patch_only_report_is_an_error(self) -> None:
        """Could-not-look is not 'not affected' — same honesty as a failed ls-tree."""
        report = SecurityReportFactory(
            project=ProjectFactory(owner="apache", repo="spark"),
            raw_text="someone said it was bad",
            title="hmm",
            proposed_patch="diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n",
        )
        executor = _PathExecutor({})

        def _dead_checkout(*_args: Any, **_kwargs: Any) -> str:
            return ""

        with (
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
            patch("franktheunicorn.security.version_map._checkout", side_effect=_dead_checkout),
        ):
            run = map_report_versions(report, _operator())

        assert run.error == ""
        assert all(r.verdict == "error" for r in run.results)
        assert all("Could not try the proposed patch" in r.summary for r in run.results)
        assert not any(c[:3] == ["git", "apply", "--check"] for c in executor.calls)

    def test_does_not_backfill_version_impact_that_contradicts_a_deep_verdict(
        self,
    ) -> None:
        report = self._report()
        kept = SecurityVerification.objects.create(
            report=report,
            branch="master",
            verdict="not-affected",
            summary="Fixed on this branch.",
            evidence="sql/core/src/Foo.scala:10",
            agent="claude/sonnet",
            version_impact=[],
        )
        executor = _PathExecutor({"master": "sql/core/src/Foo.scala\n"})
        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            map_report_versions(report, _operator())

        kept.refresh_from_db()
        assert kept.verdict == "not-affected"
        assert kept.version_impact == []
        assert kept.agent == "claude/sonnet"

    def test_replaces_a_deep_verifier_error_row_instead_of_discarding_the_cheap_result(
        self,
    ) -> None:
        """An 'error' row means the agent never got a look — nothing to protect."""
        report = self._report()
        SecurityVerification.objects.create(
            report=report,
            branch="master",
            verdict="error",
            summary="Could not prepare a checkout.",
            agent="claude/sonnet",
            version_impact=[],
        )
        executor = _PathExecutor({"master": "sql/core/src/Foo.scala\n"})
        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            map_report_versions(report, _operator())

        row = SecurityVerification.objects.get(report=report, branch="master")
        assert row.agent == VERSION_MAP_AGENT
        assert row.verdict == "affected"

    def test_no_paths_is_an_error_not_a_clean_miss(self) -> None:
        report = SecurityReportFactory(
            project=ProjectFactory(),
            raw_text="someone said it was bad",
            title="hmm",
        )
        run = map_report_versions(report, _operator())
        assert "no source paths" in run.error
        assert SecurityVerification.objects.filter(report=report).count() == 0

    def test_no_project_says_so(self) -> None:
        report = SecurityReportFactory(project=None, raw_text="sql/core/src/Foo.scala")
        run = map_report_versions(report, _operator())
        assert "no project" in run.error

    def test_unlimited_includes_every_active_version_branch(self) -> None:
        """max_branches=1 would drop branch-3.5; unlimited must not."""
        executor = _PathExecutor({})
        branches = select_branches(executor, "/w/spark", _verifier(max_branches=1), unlimited=True)
        assert "master" in branches
        assert "branch-4.0" in branches
        assert "branch-3.5" in branches
        assert "branch-2.4" not in branches


@pytest.mark.django_db
class TestVersionMapImport:
    @staticmethod
    def _archive(count: int = 2) -> Any:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for index in range(count):
                archive.writestr(
                    f"report-{index}.txt",
                    "Security vulnerability: remote code execution via unsafe "
                    f"deserialization in sql/core/src/Foo{index}.scala.",
                )
        buf.seek(0)
        return buf

    def _import(self, *, count: int, project: Any) -> Any:
        from franktheunicorn.security.zip_import import import_reports_from_zip

        config = _operator(_verifier(enabled=True))
        with patch("franktheunicorn.config.loader.get_operator_config", return_value=config):
            return import_reports_from_zip(
                self._archive(count), project=project, auto_verify_versions=True
            )

    def test_queues_one_map_per_imported_report_with_no_cap(self) -> None:
        from franktheunicorn.core.models import WorkerCommand
        from franktheunicorn.security.zip_import import MAX_AUTO_VERIFY

        project = ProjectFactory()
        result = self._import(count=MAX_AUTO_VERIFY + 1, project=project)

        assert result.imported == MAX_AUTO_VERIFY + 1
        assert result.queued_version_maps == MAX_AUTO_VERIFY + 1
        assert result.version_map_skipped_reason == ""
        assert (
            WorkerCommand.objects.filter(command="map_report_versions").count()
            == MAX_AUTO_VERIFY + 1
        )
        assert "queued for version mapping" in result.summary()

    def test_not_asking_queues_nothing(self) -> None:
        from franktheunicorn.core.models import WorkerCommand
        from franktheunicorn.security.zip_import import import_reports_from_zip

        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            return_value=_operator(_verifier(enabled=True)),
        ):
            result = import_reports_from_zip(self._archive(), project=ProjectFactory())
        assert result.queued_version_maps == 0
        assert WorkerCommand.objects.filter(command="map_report_versions").count() == 0


@pytest.mark.django_db
def test_worker_handler_logs_a_declined_run() -> None:
    from franktheunicorn.core.models import WorkerCommand
    from franktheunicorn.worker.commands import _dispatch

    report = SecurityReportFactory(project=ProjectFactory())
    cmd = WorkerCommand.objects.create(command="map_report_versions", security_report=report)
    _dispatch(cmd, _operator(_verifier(enabled=False)))
    assert "enabled is false" in cmd.log
