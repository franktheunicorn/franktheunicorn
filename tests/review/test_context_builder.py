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
        # The hunk's own lines are in the window (the excerpt reads the FILE,
        # so the changed line is the file's line 997, not the diff's "+…").
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
        cfg = ContextConfig(per_file_token_cap=20, include_first_party_imports=False)
        full, _imports = build_context_strings(
            ["myapp/big.py"], fake_repo, cfg, diff=self.DIFF_AT_1000
        )
        # 20 tokens ≈ 80 chars: the window is cut to the budget, not past it.
        excerpt = full.split("(excerpt around the change; full file is 2000 lines)", 1)[1]
        assert estimate_tokens(excerpt) <= 20 + 2  # headers cost a few tokens

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
