"""Tests for the backport / cherry-pick faithfulness sub-check."""

from __future__ import annotations

import difflib
from typing import Any
from unittest.mock import MagicMock

import pytest

from franktheunicorn.backends.base import ForgeClient
from franktheunicorn.config.models import BackportConfig
from franktheunicorn.review.checks.backport import (
    BackportCheck,
    BackportReference,
    compare_diffs,
    detect_backport_references,
)


def _file_diff(path: str, old: list[str], new: list[str]) -> str:
    """Build a git-style unified diff for one file from old/new line lists."""
    body = "\n".join(
        difflib.unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")
    )
    return f"diff --git a/{path} b/{path}\n{body}\n"


def _diff(*file_diffs: str) -> str:
    return "".join(file_diffs)


# --- reference detection ----------------------------------------------------


class TestDetectBackportReferences:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Backport of #123", "#123"),
            ("backport of #123", "#123"),
            ("Cherry-pick of #45", "#45"),
            ("cherry pick of #45", "#45"),
            ("Backport from #7", "#7"),
            ("Backported #9", "#9"),
            ("This backports #10 to 3.5", "#10"),
            ("Cherry-picking #11", "#11"),
        ],
    )
    def test_pr_reference_forms(self, text: str, expected: str) -> None:
        refs = detect_backport_references(text, "apache", "spark")
        assert len(refs) == 1
        assert refs[0].kind == "pr"
        assert refs[0].describe() == expected

    def test_cross_repo_reference(self) -> None:
        refs = detect_backport_references("Backport of apache/spark#88", "apache", "spark")
        assert len(refs) == 1
        # Same repo as the project → not flagged cross-repo.
        assert refs[0].cross_repo is False
        assert refs[0].owner == "apache"
        assert refs[0].number == 88

    def test_cross_repo_reference_other_repo(self) -> None:
        refs = detect_backport_references("Backport of other/proj#88", "apache", "spark")
        assert len(refs) == 1
        assert refs[0].cross_repo is True
        assert refs[0].owner == "other"
        assert refs[0].repo == "proj"
        assert refs[0].describe() == "other/proj#88"

    @pytest.mark.parametrize(
        ("text", "expected_sha"),
        [
            # Explicitly qualified short hex.
            ("cherry-pick of commit abc1234ef", "abc1234ef"),
            ("Cherry-pick of sha abc1234", "abc1234"),
            ("Backport. Cherry-picked from commit deadbeef1234", "deadbeef1234"),
            # A full 40-char SHA needs no qualifier.
            ("Backport of " + "a" * 40, "a" * 40),
        ],
    )
    def test_sha_reference_forms(self, text: str, expected_sha: str) -> None:
        refs = detect_backport_references(text, "apache", "spark")
        assert len(refs) == 1
        assert refs[0].kind == "sha"
        assert refs[0].sha == expected_sha

    @pytest.mark.parametrize(
        "text",
        [
            # FIX 6: a short, unqualified hex token in prose is not a commit ref.
            "Backport of deadbeef cleanup for 3.5",
            "Cherry-pick of abc1234 improves speed",
            "This backport touches file abcdef1 only",
        ],
    )
    def test_unqualified_short_sha_is_not_a_reference(self, text: str) -> None:
        assert detect_backport_references(text, "apache", "spark") == []

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Just a normal PR fixing a bug",
            "We should backport this someday, see #99",
            "Refers to #123 but is not a backport",
            "backport this branch feature-x please",
        ],
    )
    def test_no_reference_is_noop(self, text: str) -> None:
        assert detect_backport_references(text, "apache", "spark") == []

    def test_multiple_references_ordered_and_deduped(self) -> None:
        text = "Backport of #1 and cherry-pick of #2. Also backport of #1 again."
        refs = detect_backport_references(text, "apache", "spark")
        assert [r.describe() for r in refs] == ["#1", "#2"]

    # FIX 2: the source ref need not sit immediately after the cue.
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Backport fix to 3.5\nFrom #123", "#123"),
            ("This is a backport. Original: #123", "#123"),
            ("A cherry-pick.\nsource: #77", "#77"),
            ("Backport to release branch.\ncherry-picked from other/proj#9", "other/proj#9"),
        ],
    )
    def test_non_adjacent_source_ref_when_declared(self, text: str, expected: str) -> None:
        refs = detect_backport_references(text, "apache", "spark")
        assert len(refs) == 1
        assert refs[0].describe() == expected

    # FIX A: "of" and mid-sentence "original" are common in prose and must NOT
    # be treated as a backport source, even inside a declared backport.
    @pytest.mark.parametrize(
        "text",
        [
            "This is a backport. Part of #123.",
            "This backport fails because of #123.",
            "This is a backport. The fix is a variation of #123.",
            "This backport restores the original #123 behavior.",
        ],
    )
    def test_of_and_prose_original_not_a_source(self, text: str) -> None:
        assert detect_backport_references(text, "apache", "spark") == []

    def test_fixes_closes_is_not_a_backport_source(self) -> None:
        # Declared backport, but the only refs are fixes/closes → those are NOT
        # backport sources, so nothing resolvable → no-op.
        assert detect_backport_references("This backport. Fixes #99. Closes #100", "o", "r") == []

    def test_fixes_ref_ignored_alongside_real_source(self) -> None:
        refs = detect_backport_references("Backport of #5. Fixes #99.", "apache", "spark")
        assert [r.describe() for r in refs] == ["#5"]


# --- diff-of-diffs comparison ----------------------------------------------


class TestCompareDiffs:
    def _config(self, **overrides: Any) -> BackportConfig:
        return BackportConfig(**overrides)

    def test_identical_diffs_no_findings(self) -> None:
        diff = _file_diff("foo.py", ["a", "b", "c"], ["a", "X", "b", "c"])
        findings = compare_diffs(diff, diff, ignore_paths=[], config=self._config())
        assert findings == []

    def test_reordered_files_no_false_positive(self) -> None:
        d1 = _file_diff("foo.py", ["a"], ["a", "X"])
        d2 = _file_diff("bar.py", ["p"], ["p", "Y"])
        source = _diff(d1, d2)
        backport = _diff(d2, d1)  # same changes, different file order
        findings = compare_diffs(source, backport, ignore_paths=[], config=self._config())
        assert findings == []

    def test_whitespace_only_difference_no_false_positive(self) -> None:
        source = _file_diff(
            "foo.py", ["def f():", "    pass"], ["def f():", "    return 1", "    pass"]
        )
        # Backport reindents the added line with a tab / extra spaces.
        backport = _file_diff(
            "foo.py", ["def f():", "\tpass"], ["def f():", "\t\treturn  1", "\tpass"]
        )
        findings = compare_diffs(source, backport, ignore_paths=[], config=self._config())
        assert findings == []

    def test_missing_file(self) -> None:
        foo = _file_diff("foo.py", ["a"], ["a", "X"])
        bar = _file_diff("bar.py", ["p"], ["p", "Y"])
        source = _diff(foo, bar)
        backport = _diff(foo)  # bar.py missing
        findings = compare_diffs(source, backport, ignore_paths=[], config=self._config())
        assert len(findings) == 1
        assert findings[0].file_path == "bar.py"
        assert findings[0].severity == "important"
        assert "missing" in findings[0].title

    def test_extra_file(self) -> None:
        foo = _file_diff("foo.py", ["a"], ["a", "X"])
        baz = _file_diff("baz.py", ["m"], ["m", "Z"])
        source = _diff(foo)
        backport = _diff(foo, baz)  # baz.py extra
        findings = compare_diffs(source, backport, ignore_paths=[], config=self._config())
        assert len(findings) == 1
        assert findings[0].file_path == "baz.py"
        assert findings[0].severity == "nit"
        assert "not changed in source" in findings[0].title

    def test_altered_hunk_in_shared_file(self) -> None:
        source = _file_diff("foo.py", ["a", "b"], ["a", "SOURCE_LINE", "b"])
        backport = _file_diff("foo.py", ["a", "b"], ["a", "BACKPORT_LINE", "b"])
        findings = compare_diffs(source, backport, ignore_paths=[], config=self._config())
        assert len(findings) == 1
        assert findings[0].file_path == "foo.py"
        assert "differs from source" in findings[0].title
        # Missing source line drives the important severity.
        assert findings[0].severity == "important"

    def test_ignore_paths_suppresses_divergence(self) -> None:
        foo = _file_diff("foo.py", ["a"], ["a", "X"])
        changelog = _file_diff("CHANGELOG.md", ["v1"], ["v1", "v2"])
        source = _diff(foo)
        backport = _diff(foo, changelog)  # extra changelog change
        findings = compare_diffs(
            source, backport, ignore_paths=["CHANGELOG.md"], config=self._config()
        )
        assert findings == []

    def test_ignore_paths_glob_dir_prefix(self) -> None:
        foo = _file_diff("foo.py", ["a"], ["a", "X"])
        doc = _file_diff("docs/notes.md", ["d"], ["d", "e"])
        source = _diff(foo)
        backport = _diff(foo, doc)
        findings = compare_diffs(source, backport, ignore_paths=["docs/"], config=self._config())
        assert findings == []

    def test_warn_flags_gate_findings(self) -> None:
        foo = _file_diff("foo.py", ["a"], ["a", "X"])
        bar = _file_diff("bar.py", ["p"], ["p", "Y"])
        source = _diff(foo, bar)
        backport = _diff(foo)  # bar.py missing
        # Disable missing-hunk warnings → no finding.
        cfg = self._config(warn_on_missing_hunks=False)
        assert compare_diffs(source, backport, ignore_paths=[], config=cfg) == []

    def test_non_diff_source_yields_single_info_not_per_file(self) -> None:
        # FIX B: compare_diffs must honor parse status too — a non-diff source
        # body returns one info finding, never per-file 'not changed in source'.
        html = "<!DOCTYPE html><html><body>rate limited</body></html>"
        backport = _diff(
            _file_diff("foo.py", ["a"], ["a", "X"]),
            _file_diff("bar.py", ["p"], ["p", "Y"]),
        )
        findings = compare_diffs(html, backport, ignore_paths=[], config=self._config())
        assert len(findings) == 1
        assert findings[0].severity == "informational"
        assert findings[0].file_path == ""
        assert not any("not changed in source" in f.title for f in findings)

    def test_multiset_catches_dropped_duplicate_line(self) -> None:
        # FIX 3: source adds an identical line twice; backport adds it once.
        # A set-based comparison would treat these as equal; a multiset must
        # flag the dropped occurrence.
        source = _file_diff("foo.py", ["a"], ["a", "dup", "dup"])
        backport = _file_diff("foo.py", ["a"], ["a", "dup"])
        findings = compare_diffs(source, backport, ignore_paths=[], config=self._config())
        assert len(findings) == 1
        assert findings[0].file_path == "foo.py"
        assert "differs from source" in findings[0].title
        assert "1 changed line(s) present in the source are missing" in findings[0].body


# --- scan() -----------------------------------------------------------------


@pytest.mark.django_db
class TestBackportCheckScan:
    def _pr(self, **kwargs: Any) -> Any:
        from tests.factories import PullRequestFactory

        return PullRequestFactory(**kwargs)

    def _forge(self) -> MagicMock:
        return MagicMock(spec=ForgeClient)

    def test_not_a_backport_is_noop(self) -> None:
        pr = self._pr(title="Fix a bug", body="Nothing special")
        check = BackportCheck(forge_client=self._forge())
        assert check.scan(pr, "some diff", MagicMock()) == []

    def test_disabled_config_is_noop(self) -> None:
        pr = self._pr(title="Backport of #5", body="")
        check = BackportCheck(config=BackportConfig(enabled=False), forge_client=self._forge())
        assert check.scan(pr, "diff", MagicMock()) == []

    def test_faithful_backport_no_findings(self) -> None:
        foo = _file_diff("foo.py", ["a"], ["a", "X"])
        pr = self._pr(title="Backport of #5", body="", number=200)
        forge = self._forge()
        forge.get_pull_request_diff.return_value = foo
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, foo, MagicMock())
        assert findings == []
        forge.get_pull_request_diff.assert_called_once()

    def test_divergent_backport_produces_findings(self) -> None:
        source = _diff(
            _file_diff("foo.py", ["a"], ["a", "X"]),
            _file_diff("bar.py", ["p"], ["p", "Y"]),
        )
        backport = _diff(_file_diff("foo.py", ["a"], ["a", "X"]))
        pr = self._pr(title="Backport of #5", body="", number=201)
        forge = self._forge()
        forge.get_pull_request_diff.return_value = source
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, backport, MagicMock())
        assert any(f.file_path == "bar.py" for f in findings)

    def test_sha_reference_uses_get_commit_diff(self) -> None:
        foo = _file_diff("foo.py", ["a"], ["a", "X"])
        pr = self._pr(title="Cherry-pick of commit abc1234ef", body="", number=202)
        forge = self._forge()
        forge.get_commit_diff.return_value = foo
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, foo, MagicMock())
        assert findings == []
        forge.get_commit_diff.assert_called_once()
        args = forge.get_commit_diff.call_args[0]
        assert args[2] == "abc1234ef"

    def test_fetch_failure_returns_single_info_finding(self) -> None:
        pr = self._pr(title="Backport of #5", body="", number=203)
        forge = self._forge()
        forge.get_pull_request_diff.side_effect = RuntimeError("404 not found")
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, "diff", MagicMock())
        assert len(findings) == 1
        assert findings[0].severity == "informational"
        assert "could not be" in findings[0].body
        assert "404 not found" in findings[0].body

    def test_no_forge_client_returns_info_finding(self) -> None:
        pr = self._pr(title="Backport of #5", body="", number=204)
        check = BackportCheck(forge_client=None)
        findings = check.scan(pr, "diff", MagicMock())
        assert len(findings) == 1
        assert findings[0].severity == "informational"

    def test_empty_source_diff_returns_info_finding(self) -> None:
        pr = self._pr(title="Backport of #5", body="", number=205)
        forge = self._forge()
        forge.get_pull_request_diff.return_value = "   \n"
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, "diff", MagicMock())
        assert len(findings) == 1
        assert findings[0].severity == "informational"

    def test_multiple_refs_adds_note(self) -> None:
        foo = _file_diff("foo.py", ["a"], ["a", "X"])
        pr = self._pr(
            title="Backport of #5 and cherry-pick of #6",
            body="",
            number=206,
        )
        forge = self._forge()
        forge.get_pull_request_diff.return_value = foo
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, foo, MagicMock())
        notes = [f for f in findings if "multiple source references" in f.title]
        assert len(notes) == 1
        assert "#6" in notes[0].body

    def test_non_diff_source_body_single_info_finding(self) -> None:
        # FIX 1: a 200 that is an HTML interstitial (not a diff) must produce
        # exactly ONE informational finding, never per-file spam.
        html = "<!DOCTYPE html><html><body>Rate limit exceeded</body></html>"
        backport = _diff(
            _file_diff("foo.py", ["a"], ["a", "X"]),
            _file_diff("bar.py", ["p"], ["p", "Y"]),
        )
        pr = self._pr(title="Backport of #5", body="", number=207)
        forge = self._forge()
        forge.get_pull_request_diff.return_value = html
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, backport, MagicMock())
        assert len(findings) == 1
        assert findings[0].severity == "informational"
        assert "could not be parsed as a diff" in findings[0].body

    def test_diff_marker_but_unparseable_source_single_info_finding(self) -> None:
        # FIX 1: a body carrying a diff marker but no real files still parses to
        # empty → single info finding, not per-file findings.
        looks_like_diff_but_empty = "diff --git a/x b/x\n(no hunks here, truncated)\n"
        backport = _diff(_file_diff("foo.py", ["a"], ["a", "X"]))
        pr = self._pr(title="Backport of #5", body="", number=208)
        forge = self._forge()
        forge.get_pull_request_diff.return_value = looks_like_diff_but_empty
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, backport, MagicMock())
        assert len(findings) == 1
        assert findings[0].severity == "informational"
        assert "could not be parsed as a diff" in findings[0].body

    def test_oversize_source_diff_returns_info_finding(self) -> None:
        # FIX 4: an over-threshold source diff short-circuits with one finding.
        big = _file_diff("foo.py", ["a"], ["a"] + [f"line{i}" for i in range(100)])
        assert len(big) > 50
        pr = self._pr(title="Backport of #5", body="", number=209)
        forge = self._forge()
        forge.get_pull_request_diff.return_value = big
        check = BackportCheck(config=BackportConfig(max_source_diff_chars=50), forge_client=forge)
        findings = check.scan(pr, big, MagicMock())
        assert len(findings) == 1
        assert findings[0].severity == "informational"
        assert "too large to verify" in findings[0].body

    def test_get_pull_request_diff_none_returns_info_finding(self) -> None:
        # FIX 5: a forge that returns None (unsupported) → clear info finding,
        # never a NoneType crash.
        pr = self._pr(title="Backport of #5", body="", number=210)
        forge = self._forge()
        forge.get_pull_request_diff.return_value = None
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, "diff", MagicMock())
        assert len(findings) == 1
        assert findings[0].severity == "informational"
        assert "does not support" in findings[0].body

    def test_unsupported_get_commit_diff_returns_info_finding(self) -> None:
        # FIX 5: get_commit_diff raising NotImplementedError → info finding.
        pr = self._pr(title="Cherry-pick of commit abc1234ef", body="", number=211)
        forge = self._forge()
        forge.get_commit_diff.side_effect = NotImplementedError("nope")
        check = BackportCheck(forge_client=forge)
        findings = check.scan(pr, "diff", MagicMock())
        assert len(findings) == 1
        assert findings[0].severity == "informational"
        assert "does not support fetching a commit diff" in findings[0].body


# --- integration through run_enabled_checks ---------------------------------


@pytest.mark.django_db
class TestBackportThroughRunner:
    def test_forge_client_threaded_and_category_backport(self) -> None:
        from franktheunicorn.config.models import ProjectConfig
        from franktheunicorn.review.checks import run_enabled_checks
        from tests.factories import ProjectFactory, PullRequestFactory

        project = ProjectFactory(owner="apache", repo="spark")
        pr = PullRequestFactory(
            project=project,
            title="Backport of #5",
            body="",
            number=300,
        )
        source = _diff(
            _file_diff("foo.py", ["a"], ["a", "X"]),
            _file_diff("bar.py", ["p"], ["p", "Y"]),
        )
        backport = _diff(_file_diff("foo.py", ["a"], ["a", "X"]))

        forge = MagicMock(spec=ForgeClient)
        forge.get_pull_request_diff.return_value = source

        config = ProjectConfig(owner="apache", repo="spark", llm_checks=["backport"])
        drafts = run_enabled_checks(
            pr,
            backport,
            project_config=config,
            forge_client=forge,
        )
        assert drafts
        assert all("check:backport" in d.sources for d in drafts)
        assert all(d.category == "backport" for d in drafts)
        assert any(d.file_path == "bar.py" for d in drafts)


class TestRegistryAndConfig:
    def test_backport_in_registry(self) -> None:
        from franktheunicorn.review.checks import _get_registry

        registry = _get_registry()
        assert "backport" in registry
        assert registry["backport"] is BackportCheck

    def test_backport_is_known_check(self, caplog: pytest.LogCaptureFixture) -> None:
        from franktheunicorn.config.models import ProjectConfig

        with caplog.at_level("WARNING"):
            ProjectConfig(owner="x", repo="y", llm_checks=["backport"])
        assert not any("Unknown llm_check" in r.message for r in caplog.records)

    def test_reference_describe_pr_and_sha(self) -> None:
        pr_ref = BackportReference(kind="pr", owner="o", repo="r", number=9)
        assert pr_ref.describe() == "#9"
        sha_ref = BackportReference(kind="sha", owner="o", repo="r", sha="abcdef1234567")
        assert sha_ref.describe() == "commit abcdef123456"

    def test_max_source_diff_chars_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_source_diff_chars must be positive"):
            BackportConfig(max_source_diff_chars=0)
