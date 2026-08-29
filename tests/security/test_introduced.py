"""Tests for git-history introduction dating.

The interesting question is not "did git run" but "does a floor get reported as
an answer" — a file-add date presented as the introduction date would date a
2013 file for a hole added in 2024.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.review.tool_executor import ExecResult
from franktheunicorn.security.introduced import (
    find_introduction,
    patch_needles,
    persist_introduction,
)
from tests.factories import ProjectFactory, SecurityReportFactory
from tests.security.test_verifier import _BRANCH_LISTING, _FakeExecutor, _operator, _verifier

_PATCH = (
    "--- a/sql/core/src/Foo.scala\n"
    "+++ b/sql/core/src/Foo.scala\n"
    "@@ -1,3 +1,3 @@\n"
    "-    val cls = Utils.classForName(userSuppliedName)\n"
    "+    val cls = Utils.classForName(checkAllowed(userSuppliedName))\n"
    "-  }\n"
)


class TestPatchNeedles:
    def test_removed_lines_longest_first(self) -> None:
        assert patch_needles(_PATCH) == ["val cls = Utils.classForName(userSuppliedName)"]

    def test_a_short_removed_line_is_not_a_needle(self) -> None:
        """A lone brace dates to the first commit and tells you nothing."""
        assert patch_needles("--- a/x\n+++ b/x\n-  }\n-\n") == []

    def test_no_patch_no_needles(self) -> None:
        assert patch_needles("") == []

    def test_a_long_line_is_capped(self) -> None:
        from franktheunicorn.security.introduced import MAX_NEEDLE_CHARS

        needles = patch_needles("--- a/x\n+++ b/x\n-" + "z" * 500 + "\n")
        assert len(needles[0]) == MAX_NEEDLE_CHARS


class _GitExecutor(_FakeExecutor):
    """Answers pickaxe, file-add and tag queries; records what it was asked."""

    _SEP = "\x1f"

    def __init__(
        self,
        *,
        pickaxe: str | None = None,
        added: str | None = None,
        tags: str = "",
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
        self.pickaxe = pickaxe
        self.added = added
        self.tags = tags
        self.needles: list[str] = []

    def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        self.calls.append(cmd)
        if cmd[:4] == ["git", "checkout", "--detach", "--force"]:
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "clean"]:
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"]:
            return ExecResult(returncode=0, stdout="headsha\n", stderr="")
        if cmd[:2] == ["git", "tag"]:
            return ExecResult(returncode=0, stdout=self.tags, stderr="")
        if cmd[:2] == ["git", "log"] and "-S" in cmd:
            self.needles.append(cmd[cmd.index("-S") + 1])
            return ExecResult(returncode=0, stdout=self.pickaxe or "", stderr="")
        if cmd[:2] == ["git", "log"] and "--diff-filter=A" in cmd:
            return ExecResult(returncode=0, stdout=self.added or "", stderr="")
        return super().run(cmd, cwd, timeout, stdin)


def _log_line(sha: str, stamp: int, subject: str) -> str:
    return f"{sha}\x1f{stamp}\x1f{subject}\n"


@pytest.mark.django_db
class TestFindIntroduction:
    def _report(self, **kwargs: Any) -> Any:
        defaults = {
            "project": ProjectFactory(owner="apache", repo="spark"),
            "raw_text": "Unsafe class loading in sql/core/src/Foo.scala",
            "proposed_patch": _PATCH,
        }
        return SecurityReportFactory(**{**defaults, **kwargs})

    def _run(self, executor: Any, report: Any) -> Any:
        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            return find_introduction(report, _operator())

    def test_pickaxes_the_removed_line_and_lists_release_tags(self) -> None:
        executor = _GitExecutor(
            # git log is newest-first, so the introducing commit is the LAST line.
            pickaxe=_log_line("newsha", 1_700_000_000, "Refactor loader")
            + _log_line("oldsha1234567890", 1_400_000_000, "Add dynamic class loading"),
            tags="v3.5.0\nv4.0.0\nv3.5.0-rc1\n",
        )
        run = self._run(executor, self._report())

        assert run.error == ""
        assert run.commit == "oldsha1234567890"
        assert run.method == "patch-line"
        assert run.when == datetime.fromtimestamp(1_400_000_000, tz=UTC)
        # -rc1 is not a release a maintainer publishes an advisory against.
        assert run.releases == ["v3.5.0", "v4.0.0"]
        assert "Add dynamic class loading" in run.summary()
        assert executor.needles == ["val cls = Utils.classForName(userSuppliedName)"]

    def test_falls_back_to_file_added_and_says_it_is_a_floor(self) -> None:
        """No patch means we can only date the file, which is not the same answer."""
        executor = _GitExecutor(added=_log_line("addsha", 1_300_000_000, "Add Foo.scala"))
        run = self._run(executor, self._report(proposed_patch=""))

        assert run.commit == "addsha"
        assert run.method == "file-added"
        assert "Cited file first added" in run.summary()
        assert not any("-S" in c for c in executor.calls)

    def test_a_pickaxed_origin_beats_a_file_add_one(self) -> None:
        """Taking the newest of a mixed set would report a floor as the answer."""
        report = self._report(
            raw_text="Bug in sql/core/src/Foo.scala and also core/src/Ancient.scala"
        )
        executor = _GitExecutor(
            pickaxe=_log_line("pickaxed", 1_400_000_000, "Introduce the hole"),
            # A *newer* file-add that must not win.
            added=_log_line("newfile", 1_900_000_000, "Add an unrelated file"),
        )
        run = self._run(executor, report)

        assert run.commit == "pickaxed"
        assert run.method == "patch-line"

    def test_caps_the_release_list_but_keeps_the_count(self) -> None:
        """Measured on apache/spark: one 2014 commit is contained by 254 tags, 86 of
        them real releases. The list is not the answer; the earliest plus the count is."""
        from franktheunicorn.security.introduced import MAX_RELEASES

        tags = "".join(f"v1.{minor}.0\n" for minor in range(40))
        executor = _GitExecutor(pickaxe=_log_line("sha", 1_400_000_000, "Introduce it"), tags=tags)
        run = self._run(executor, self._report())

        assert run.release_count == 40
        assert len(run.releases) == MAX_RELEASES
        assert run.releases[0] == "v1.0.0"
        assert "Present in 40 release(s), from v1.0.0 onwards" in run.summary()

    def test_release_candidate_tags_are_not_releases(self) -> None:
        """Spark's tag list is two thirds rc/preview; an advisory names v3.5.0."""
        executor = _GitExecutor(
            pickaxe=_log_line("sha", 1_400_000_000, "x"),
            tags="2.0.0-preview\nv3.5.0-rc1\nv3.5.0\nv4.0.0\n",
        )
        run = self._run(executor, self._report())
        assert run.releases == ["v3.5.0", "v4.0.0"]

    def test_no_release_tag_says_unreleased(self) -> None:
        executor = _GitExecutor(pickaxe=_log_line("sha", 1_700_000_000, "Recent change"), tags="")
        run = self._run(executor, self._report())

        assert run.releases == []
        assert "unreleased so far" in run.summary()

    def test_undatable_paths_is_not_an_error_but_says_so(self) -> None:
        executor = _GitExecutor(pickaxe="", added="")
        run = self._run(executor, self._report())

        assert run.error == ""
        assert run.commit == ""
        assert "could not date any cited path" in run.summary()

    def test_searches_the_default_branch_not_a_release_branch(self) -> None:
        """A release branch's log stops at the fork, dating the hole to the branch."""
        executor = _GitExecutor(pickaxe=_log_line("sha", 1_400_000_000, "x"))
        self._run(executor, self._report())

        checkouts = [c for c in executor.calls if c[:2] == ["git", "checkout"]]
        assert checkouts
        assert all(c[-1] == "origin/master" for c in checkouts)

    def test_verifier_disabled_says_so(self) -> None:
        report = self._report()
        with patch("franktheunicorn.review.tool_executor.make_executor"):
            run = find_introduction(report, _operator(_verifier(enabled=False)))
        assert "enabled is false" in run.error

    def test_no_project_says_so(self) -> None:
        run = find_introduction(
            SecurityReportFactory(project=None, raw_text="sql/core/src/Foo.scala"), _operator()
        )
        assert "no project" in run.error

    def test_no_paths_is_an_error_not_a_clean_miss(self) -> None:
        report = self._report(raw_text="someone said it was bad", title="hmm", proposed_patch="")
        run = find_introduction(report, _operator())
        assert "no source paths" in run.error


@pytest.mark.django_db
class TestPersistIntroduction:
    def test_stores_the_answer_on_the_report(self) -> None:
        report = SecurityReportFactory(project=ProjectFactory())
        executor = _GitExecutor(
            pickaxe=_log_line("abcdef1234567890", 1_400_000_000, "Introduce it"),
            tags="v3.5.0\n",
        )
        report.raw_text = "hole in sql/core/src/Foo.scala"
        report.proposed_patch = _PATCH
        report.save(update_fields=["raw_text", "proposed_patch"])

        with patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor):
            run = find_introduction(report, _operator())
        persist_introduction(report, run)

        report.refresh_from_db()
        assert report.introduced_commit == "abcdef1234567890"
        assert report.introduced_method == "patch-line"
        assert report.introduced_releases == ["v3.5.0"]
        assert "sql/core/src/Foo.scala" in report.introduced_summary

    def test_a_failed_scan_does_not_wipe_an_earlier_answer(self) -> None:
        report = SecurityReportFactory(
            project=ProjectFactory(),
            introduced_commit="keepme",
            introduced_method="patch-line",
            introduced_releases=["v3.5.0"],
        )
        failed = find_introduction(report, _operator(_verifier(enabled=False)))
        persist_introduction(report, failed)

        report.refresh_from_db()
        assert report.introduced_commit == "keepme"
        assert report.introduced_releases == ["v3.5.0"]


@pytest.mark.django_db
def test_worker_handler_logs_a_declined_run() -> None:
    from franktheunicorn.core.models import WorkerCommand
    from franktheunicorn.worker.commands import _dispatch

    report = SecurityReportFactory(project=ProjectFactory())
    cmd = WorkerCommand.objects.create(command="find_report_introduction", security_report=report)
    _dispatch(cmd, _operator(_verifier(enabled=False)))
    assert "enabled is false" in cmd.log


@pytest.mark.django_db
class TestIntroductionImport:
    """The checkbox and --find-introduction path."""

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

    def _import(self, *, project: Any, **kwargs: Any) -> Any:
        from franktheunicorn.security.zip_import import import_reports_from_zip

        config = _operator(_verifier(enabled=True))
        with patch("franktheunicorn.config.loader.get_operator_config", return_value=config):
            return import_reports_from_zip(self._archive(), project=project, **kwargs)

    def test_queues_one_scan_per_imported_report(self) -> None:
        from franktheunicorn.core.models import WorkerCommand

        result = self._import(project=ProjectFactory(), auto_find_introduction=True)

        assert result.queued_introduction_scans == 2
        assert result.introduction_skipped_reason == ""
        assert WorkerCommand.objects.filter(command="find_report_introduction").count() == 2
        assert "queued for introduction dating" in result.summary()

    def test_not_asking_queues_nothing(self) -> None:
        from franktheunicorn.core.models import WorkerCommand

        result = self._import(project=ProjectFactory())

        assert result.queued_introduction_scans == 0
        assert WorkerCommand.objects.filter(command="find_report_introduction").count() == 0

    def test_a_report_with_no_project_says_why_it_was_skipped(self) -> None:
        result = self._import(project=None, auto_find_introduction=True)

        assert result.queued_introduction_scans == 0
        assert "no project" in result.introduction_skipped_reason
