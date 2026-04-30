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
