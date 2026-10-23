"""Tests for the full-file + first-party-import context builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from franktheunicorn.config.models import ContextConfig
from franktheunicorn.review.context_builder import build_context_strings, estimate_tokens


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Repo layout with a top-level package and a couple of changed files."""
    pkg = tmp_path / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "helpers.py").write_text("def helper():\n    return 1\n")
    (pkg / "models.py").write_text("from myapp.helpers import helper\n\n\nclass User:\n    pass\n")
    (tmp_path / "README.md").write_text("# My App\n")
    return tmp_path


class TestEstimateTokens:
    def test_chars_over_four(self) -> None:
        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("") == 0


class TestDisabled:
    def test_returns_empty_when_disabled(self, fake_repo: Path) -> None:
        cfg = ContextConfig(include_full_file=False, include_first_party_imports=False)
        full, imports = build_context_strings(["myapp/models.py"], fake_repo, cfg)
        assert full == ""
        assert imports == ""

    def test_returns_empty_when_repo_path_missing(self) -> None:
        cfg = ContextConfig()
        full, imports = build_context_strings(["a.py"], None, cfg)
        assert full == ""
        assert imports == ""

    def test_returns_empty_when_repo_path_does_not_exist(self, tmp_path: Path) -> None:
        cfg = ContextConfig()
        full, imports = build_context_strings(["a.py"], tmp_path / "nope", cfg)
        assert full == ""
        assert imports == ""


class TestFullFileSection:
    def test_includes_changed_file_content(self, fake_repo: Path) -> None:
        cfg = ContextConfig(include_first_party_imports=False)
        full, _imports = build_context_strings(["myapp/models.py"], fake_repo, cfg)
        assert "## Full file context" in full
        assert "myapp/models.py" in full
        assert "class User" in full

    def test_skips_missing_files(self, fake_repo: Path) -> None:
        cfg = ContextConfig(include_first_party_imports=False)
        full, _imports = build_context_strings(
            ["myapp/does_not_exist.py", "myapp/helpers.py"], fake_repo, cfg
        )
        assert "does_not_exist" not in full
        assert "helpers.py" in full

    def test_per_file_cap_skips_oversized(self, fake_repo: Path) -> None:
        big = fake_repo / "myapp" / "big.py"
        big.write_text("# " + "x" * 100_000)
        cfg = ContextConfig(per_file_token_cap=10, include_first_party_imports=False)
        full, _imports = build_context_strings(["myapp/big.py"], fake_repo, cfg)
        assert "big.py" not in full


class TestOversizedExcerpt:
    """A file over per_file_token_cap contributes its changed hunks plus
    padding, not nothing — on a monorepo the interesting files are routinely
    10x the cap."""

    DIFF_AT_1000 = (
        "diff --git a/myapp/big.py b/myapp/big.py\n"
        "--- a/myapp/big.py\n"
        "+++ b/myapp/big.py\n"
        "@@ -997,5 +997,5 @@\n"
        " line 996\n"
        " line 997\n"
        "-old\n"
        "+line 997 changed\n"
        " line 998\n"
        " line 999\n"
    )

    @staticmethod
    def _write_big(repo: Path, lines: int = 2000) -> None:
        (repo / "myapp" / "big.py").write_text(
            "\n".join(f"line {i}" for i in range(1, lines + 1)) + "\n"
        )

    def test_oversized_file_excerpted_around_hunks(self, fake_repo: Path) -> None:
        self._write_big(fake_repo)
        cfg = ContextConfig(per_file_token_cap=100, include_first_party_imports=False)
        full, _imports = build_context_strings(
            ["myapp/big.py"], fake_repo, cfg, diff=self.DIFF_AT_1000
        )
        assert "big.py" in full
        # Announces itself as an excerpt of a known-size file.
        assert "(excerpt around the change; full file is 2000 lines)" in full
        # Target-side line numbers name lines of the checked-out file (the PR
        # head merged onto the base — see _hunk_ranges_by_file); the fixture's
        # hunk is symmetric, so source and target numbers coincide here.
        assert "line 997\n" in full
        # The window is bounded: a line 400 away from the hunk is omitted.
        assert "line 400\n" not in full
        assert "… lines 1-" in full
        assert "omitted" in full

    def test_oversized_without_hunk_info_still_skipped(self, fake_repo: Path) -> None:
        self._write_big(fake_repo)
        cfg = ContextConfig(per_file_token_cap=100, include_first_party_imports=False)
        full, _imports = build_context_strings(
            ["myapp/big.py"], fake_repo, cfg, diff="not a diff at all"
        )
        assert "big.py" not in full

    def test_excerpt_respects_token_cap(self, fake_repo: Path) -> None:
        self._write_big(fake_repo)
        # 40 tokens is enough for the bare hunk but not the padded window, so
        # the padding shrinks — and everything must still fit inside the cap
        # the caller re-checks.
        cfg = ContextConfig(per_file_token_cap=40, include_first_party_imports=False)
        full, _imports = build_context_strings(
            ["myapp/big.py"], fake_repo, cfg, diff=self.DIFF_AT_1000
        )
        excerpt = full.split("(excerpt around the change; full file is 2000 lines)", 1)[1]
        assert estimate_tokens(excerpt) <= 40

    def test_big_hunk_not_dropped_by_marker_overhead(self, fake_repo: Path) -> None:
        # A hunk big enough that its window fills the whole budget: the
        # trailing omission marker must still fit, or the caller's re-check
        # drops the file whole and the excerpt silently vanishes.
        self._write_big(fake_repo, lines=5000)
        diff = (
            "diff --git a/myapp/big.py b/myapp/big.py\n"
            "--- a/myapp/big.py\n"
            "+++ b/myapp/big.py\n"
            "@@ -1000,201 +1000,201 @@\n" + " ctx\n" * 100 + "-old\n" + "+new\n" + " ctx\n" * 100
        )
        cfg = ContextConfig(per_file_token_cap=100, include_first_party_imports=False)
        full, _imports = build_context_strings(["myapp/big.py"], fake_repo, cfg, diff=diff)
        assert "big.py" in full  # not dropped by the caller's token re-check
        excerpt = full.split("(excerpt around the change; full file is 5000 lines)", 1)[1]
        assert estimate_tokens(excerpt) <= 100

    def test_nearby_hunks_overlap_emitted_once(self, fake_repo: Path) -> None:
        # Two hunks closer than the padding merge into one window; the
        # overlap must not appear twice.
        self._write_big(fake_repo, lines=300)
        diff = (
            "diff --git a/myapp/big.py b/myapp/big.py\n"
            "--- a/myapp/big.py\n"
            "+++ b/myapp/big.py\n"
            "@@ -100,2 +100,2 @@\n"
            " line 99\n"
            "-old\n"
            "+line 100 changed\n"
            "@@ -131,2 +131,2 @@\n"
            " line 130\n"
            "-old\n"
            "+line 131 changed\n"
        )
        cfg = ContextConfig(per_file_token_cap=200, include_first_party_imports=False)
        full, _imports = build_context_strings(["myapp/big.py"], fake_repo, cfg, diff=diff)
        excerpt = full.split("(excerpt around the change; full file is 300 lines)", 1)[1]
        # No line appears twice in the excerpt body.
        body_lines = [ln for ln in excerpt.splitlines() if ln.startswith("line ")]
        assert len(body_lines) == len(set(body_lines))
        assert "line 100\n" in full
        assert "line 131\n" in full

    def test_hunk_past_eof_skipped_not_inverted(self, fake_repo: Path) -> None:
        # A checkout behind the diff's head can carry a hunk range past EOF;
        # emitting it would produce an inverted "lines 5460-5000:" header.
        self._write_big(fake_repo, lines=5000)
        diff = (
            "diff --git a/myapp/big.py b/myapp/big.py\n"
            "--- a/myapp/big.py\n"
            "+++ b/myapp/big.py\n"
            "@@ -5500,2 +5500,2 @@\n"
            " line 5499\n"
            "-old\n"
            "+line 5500 changed\n"
        )
        cfg = ContextConfig(per_file_token_cap=200, include_first_party_imports=False)
        full, _imports = build_context_strings(["myapp/big.py"], fake_repo, cfg, diff=diff)
        assert "big.py" not in full  # nothing windowable in this checkout

    def test_cut_window_header_names_only_delivered_lines(self, fake_repo: Path) -> None:
        # A hunk bigger than the remaining budget gets cut mid-window; the
        # header must say the lines actually delivered, never the window that
        # was asked for — the model grounds line citations in these headers.
        from franktheunicorn.review.context_builder import _excerpt_around

        content = "\n".join(f"line {i}" for i in range(1, 22))
        out = _excerpt_around(content, [(16, 19)], 34)
        assert "lines 16-18:" in out  # 19 did not fit…
        assert "…" in out  # …and the cut is marked
        assert "lines 16-19" not in out  # the lie is gone
        # The trailing marker resumes from the delivered end, not the asked one.
        assert "… lines 19-21 omitted" in out

    def test_two_hunks_two_windows(self, fake_repo: Path) -> None:
        self._write_big(fake_repo)
        diff = (
            "diff --git a/myapp/big.py b/myapp/big.py\n"
            "--- a/myapp/big.py\n"
            "+++ b/myapp/big.py\n"
            "@@ -100,2 +100,2 @@\n"
            " line 99\n"
            "-old\n"
            "+line 100 changed\n"
            "@@ -1500,2 +1500,2 @@\n"
            " line 1499\n"
            "-old\n"
            "+line 1500 changed\n"
        )
        cfg = ContextConfig(per_file_token_cap=200, include_first_party_imports=False)
        full, _imports = build_context_strings(["myapp/big.py"], fake_repo, cfg, diff=diff)
        assert "line 100\n" in full
        assert "line 1500\n" in full
        assert "… lines " in full  # the gap between the windows is marked
        assert "line 800\n" not in full

    def test_small_file_unaffected_by_diff_param(self, fake_repo: Path) -> None:
        cfg = ContextConfig(include_first_party_imports=False)
        full, _imports = build_context_strings(
            ["myapp/models.py"], fake_repo, cfg, diff=self.DIFF_AT_1000
        )
        assert "class User" in full
        assert "(excerpt" not in full

    def test_one_malformed_section_does_not_sink_the_rest(self, fake_repo: Path) -> None:
        # The diff is parsed per `diff --git` section: a single bad hunk
        # (here: a hunk header that promises more lines than follow) used to
        # raise for the whole diff and silently drop every file's excerpt.
        self._write_big(fake_repo)
        good = (
            "diff --git a/myapp/big.py b/myapp/big.py\n"
            "--- a/myapp/big.py\n"
            "+++ b/myapp/big.py\n"
            "@@ -997,4 +997,4 @@\n"
            " line 996\n"
            " line 997\n"
            "-old\n"
            "+line 997 changed\n"
            " line 998\n"
        )
        bad = (
            "diff --git a/myapp/other.py b/myapp/other.py\n"
            "--- a/myapp/other.py\n"
            "+++ b/myapp/other.py\n"
            "@@ -1,99 +1,99 @@\n"
            " only one line follows, not 99\n"
        )
        cfg = ContextConfig(per_file_token_cap=200, include_first_party_imports=False)
        full, _imports = build_context_strings(
            ["myapp/big.py"], fake_repo, cfg, diff=bad + "\n" + good
        )
        assert "big.py" in full  # the good section still windows
        assert "line 997\n" in full

    def test_marker_quoted_inside_a_hunk_does_not_split(self, fake_repo: Path) -> None:
        # A hunk body can quote `diff --git` (a test fixture that contains a
        # diff); sections split at LINE STARTS only, so this stays one section.
        self._write_big(fake_repo)
        diff = (
            "diff --git a/myapp/big.py b/myapp/big.py\n"
            "--- a/myapp/big.py\n"
            "+++ b/myapp/big.py\n"
            "@@ -997,4 +997,4 @@\n"
            " line 996\n"
            " line 997\n"
            "-diff --git a/quoted.py b/quoted.py\n"
            "+line 997 changed\n"
            " line 998\n"
        )
        cfg = ContextConfig(per_file_token_cap=200, include_first_party_imports=False)
        full, _imports = build_context_strings(["myapp/big.py"], fake_repo, cfg, diff=diff)
        assert "big.py" in full
        assert "line 997\n" in full

    def test_total_budget_is_respected(self, fake_repo: Path) -> None:
        # Two ~100-token files (~400 chars each) with a 50-token budget
        # means only zero or one fits; the second must be dropped.
        for name in ("a.py", "b.py"):
            (fake_repo / "myapp" / name).write_text("# " + "x" * 400 + "\n")
        cfg = ContextConfig(
            include_first_party_imports=False,
            total_token_budget=50,
            per_file_token_cap=200,
        )
        full, _imports = build_context_strings(["myapp/a.py", "myapp/b.py"], fake_repo, cfg)
        # At most one file should appear.
        appearances = full.count("# xxxx")
        assert appearances <= 1


class TestImportSection:
    def test_includes_first_party_imports(self, fake_repo: Path) -> None:
        cfg = ContextConfig(package_roots=["myapp"])
        _full, imports = build_context_strings(["myapp/models.py"], fake_repo, cfg)
        assert "## Imported modules (first-party)" in imports
        assert "helpers.py" in imports
        assert "def helper" in imports

    def test_oversized_import_head_truncated_within_cap(self, fake_repo: Path) -> None:
        # A big import contributes its head; the truncation marker is part of
        # the budget, so the section never exceeds per_file_token_cap per file.
        big = fake_repo / "myapp" / "bigdep.py"
        big.write_text("\n".join(f"dep_line_{i:05d}" for i in range(2000)) + "\n")
        (fake_repo / "myapp" / "small.py").write_text("from myapp.bigdep import thing\n")
        cfg = ContextConfig(package_roots=["myapp"], per_file_token_cap=50)
        _full, imports = build_context_strings(["myapp/small.py"], fake_repo, cfg)
        assert "bigdep.py" in imports
        assert "more lines)" in imports
        block = imports.split("### myapp/bigdep.py", 1)[1]
        body = block.split("```python\n", 1)[1].split("```", 1)[0]
        assert estimate_tokens(body) <= 50

    def test_no_imports_when_disabled(self, fake_repo: Path) -> None:
        cfg = ContextConfig(include_first_party_imports=False, package_roots=["myapp"])
        _full, imports = build_context_strings(["myapp/models.py"], fake_repo, cfg)
        assert imports == ""

    def test_does_not_double_count_changed_file(self, fake_repo: Path) -> None:
        # If models.py and helpers.py are both changed, helpers shouldn't
        # appear again as an "imported" module.
        cfg = ContextConfig(package_roots=["myapp"])
        _full, imports = build_context_strings(
            ["myapp/models.py", "myapp/helpers.py"], fake_repo, cfg
        )
        # helpers.py is in the full-file section, not imports.
        assert imports == ""

    def test_skips_third_party_imports(self, fake_repo: Path) -> None:
        (fake_repo / "myapp" / "uses_external.py").write_text("import os\nimport requests\n")
        cfg = ContextConfig(package_roots=["myapp"])
        _full, imports = build_context_strings(["myapp/uses_external.py"], fake_repo, cfg)
        assert "requests" not in imports
        assert "os" not in imports


class TestAutodetectPackageRoots:
    def test_autodetects_top_level_package(self, fake_repo: Path) -> None:
        # No explicit package_roots — autodetect should find "myapp".
        cfg = ContextConfig()
        _full, imports = build_context_strings(["myapp/models.py"], fake_repo, cfg)
        assert "helpers.py" in imports

    def test_autodetects_src_layout(self, tmp_path: Path) -> None:
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("")
        (src / "util.py").write_text("def f():\n    return 1\n")
        (src / "main.py").write_text("from pkg.util import f\n")
        cfg = ContextConfig()
        _full, imports = build_context_strings(["src/pkg/main.py"], tmp_path, cfg)
        assert "util.py" in imports

    def test_autodetects_python_dir_layout(self, tmp_path: Path) -> None:
        # Spark's shape: the Python package is nested under python/, which the
        # top-level scan never saw, so first-party imports resolved to nothing.
        pkg = tmp_path / "python" / "pyspark"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "types.py").write_text("X = 1\n")
        (pkg / "conversion.py").write_text("from pyspark.types import X\n")
        cfg = ContextConfig()
        _full, imports = build_context_strings(["python/pyspark/conversion.py"], tmp_path, cfg)
        assert "types.py" in imports
