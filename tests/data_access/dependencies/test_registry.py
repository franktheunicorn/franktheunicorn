"""Tests for the dependency parser registry."""

from __future__ import annotations

from franktheunicorn.data_access.dependencies.python_parsers import (
    PyprojectTomlParser,
    RequirementsTxtParser,
    SetupPyParser,
)
from franktheunicorn.data_access.dependencies.registry import (
    get_parser_for_file,
    is_dependency_file,
    parse_dependency_changes,
)
from franktheunicorn.data_access.github.types import PRFileChange


class TestIsDependencyFile:
    """Tests for is_dependency_file."""

    def test_python_files(self) -> None:
        assert is_dependency_file("requirements.txt")
        assert is_dependency_file("requirements-dev.txt")
        assert is_dependency_file("pyproject.toml")
        assert is_dependency_file("setup.py")
        assert is_dependency_file("setup.cfg")
        assert is_dependency_file("python/setup.py")

    def test_non_dependency_files(self) -> None:
        assert not is_dependency_file("README.md")
        assert not is_dependency_file("src/main.py")
        assert not is_dependency_file("pom.xml")  # Not yet supported
        assert not is_dependency_file("Cargo.toml")  # Not yet supported
        assert not is_dependency_file("package.json")


class TestGetParserForFile:
    """Tests for get_parser_for_file."""

    def test_returns_correct_parser_types(self) -> None:
        assert isinstance(get_parser_for_file("requirements.txt"), RequirementsTxtParser)
        assert isinstance(get_parser_for_file("pyproject.toml"), PyprojectTomlParser)
        assert isinstance(get_parser_for_file("setup.py"), SetupPyParser)

    def test_returns_none_for_unknown(self) -> None:
        assert get_parser_for_file("Makefile") is None
        assert get_parser_for_file("pom.xml") is None


class TestParseDependencyChanges:
    """Tests for parse_dependency_changes with PRFileChange tuples."""

    def test_parses_requirements_txt(self, requirements_txt_patch: str) -> None:
        files = (
            PRFileChange(
                filename="requirements.txt",
                status="modified",
                additions=2,
                deletions=2,
                patch=requirements_txt_patch,
            ),
        )
        result = parse_dependency_changes(files)
        assert len(result.transitions) == 2
        assert result.source_files == ("requirements.txt",)
        names = {t.package_name for t in result.transitions}
        assert names == {"requests", "numpy"}

    def test_skips_non_dependency_files(self) -> None:
        files = (
            PRFileChange(
                filename="src/main.py",
                status="modified",
                additions=10,
                deletions=5,
                patch="+import foo\n-import bar\n",
            ),
        )
        result = parse_dependency_changes(files)
        assert len(result.transitions) == 0
        assert result.source_files == ()

    def test_skips_files_with_no_patch(self) -> None:
        files = (
            PRFileChange(
                filename="requirements.txt",
                status="modified",
                additions=1,
                deletions=1,
                patch="",
            ),
        )
        result = parse_dependency_changes(files)
        assert len(result.transitions) == 0

    def test_multiple_dependency_files(
        self, requirements_txt_patch: str, spark_setup_py_patch: str
    ) -> None:
        files = (
            PRFileChange(
                filename="requirements.txt",
                status="modified",
                patch=requirements_txt_patch,
            ),
            PRFileChange(
                filename="python/setup.py",
                status="modified",
                patch=spark_setup_py_patch,
            ),
        )
        result = parse_dependency_changes(files)
        # 2 from requirements.txt + 1 from setup.py
        assert len(result.transitions) == 3
        assert set(result.source_files) == {"requirements.txt", "python/setup.py"}
