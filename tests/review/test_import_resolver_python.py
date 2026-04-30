"""Tests for the Python first-party import resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from franktheunicorn.review.import_resolvers import get_resolver
from franktheunicorn.review.import_resolvers.python import PythonImportResolver


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("def a():\n    return 1\n")
    (pkg / "b.py").write_text("from pkg.a import a\n")
    sub = pkg / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "c.py").write_text("from ..a import a\n")
    return tmp_path


class TestPythonImportResolver:
    def test_absolute_import(self, repo: Path) -> None:
        resolver = PythonImportResolver()
        result = resolver.resolve(repo / "pkg" / "b.py", repo, ["pkg"])
        assert (repo / "pkg" / "a.py") in result

    def test_relative_import(self, repo: Path) -> None:
        resolver = PythonImportResolver()
        result = resolver.resolve(repo / "pkg" / "sub" / "c.py", repo, ["pkg"])
        assert (repo / "pkg" / "a.py") in result

    def test_filters_third_party(self, repo: Path) -> None:
        target = repo / "pkg" / "external.py"
        target.write_text("import os\nimport requests\nfrom pkg.a import a\n")
        resolver = PythonImportResolver()
        result = resolver.resolve(target, repo, ["pkg"])
        assert (repo / "pkg" / "a.py") in result
        assert all("requests" not in str(p) for p in result)
        assert all(p.name != "os.py" for p in result)

    def test_empty_package_roots_returns_nothing(self, repo: Path) -> None:
        resolver = PythonImportResolver()
        assert resolver.resolve(repo / "pkg" / "b.py", repo, []) == []

    def test_syntax_error_returns_empty(self, repo: Path) -> None:
        broken = repo / "pkg" / "broken.py"
        broken.write_text("def )(:\n  garbage\n")
        resolver = PythonImportResolver()
        assert resolver.resolve(broken, repo, ["pkg"]) == []

    def test_missing_file_returns_empty(self, repo: Path) -> None:
        resolver = PythonImportResolver()
        assert resolver.resolve(repo / "pkg" / "ghost.py", repo, ["pkg"]) == []

    def test_src_layout(self, tmp_path: Path) -> None:
        src_pkg = tmp_path / "src" / "pkg"
        src_pkg.mkdir(parents=True)
        (src_pkg / "__init__.py").write_text("")
        (src_pkg / "x.py").write_text("def x():\n    return 1\n")
        (src_pkg / "y.py").write_text("from pkg.x import x\n")
        resolver = PythonImportResolver()
        result = resolver.resolve(src_pkg / "y.py", tmp_path, ["pkg"])
        assert (src_pkg / "x.py") in result


class TestRegistry:
    def test_get_resolver_for_python(self, tmp_path: Path) -> None:
        assert get_resolver(tmp_path / "foo.py") is not None

    def test_get_resolver_unknown_extension(self, tmp_path: Path) -> None:
        assert get_resolver(tmp_path / "foo.go") is None
