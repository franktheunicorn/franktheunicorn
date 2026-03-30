"""Tests for Python dependency diff parsers."""

from __future__ import annotations

from franktheunicorn.data_access.dependencies.python_parsers import (
    PyprojectTomlParser,
    RequirementsTxtParser,
    SetupPyParser,
)
from franktheunicorn.data_access.dependencies.types import Ecosystem


class TestRequirementsTxtParser:
    """Tests for RequirementsTxtParser."""

    def test_matches_requirements_txt(self, requirements_parser: RequirementsTxtParser) -> None:
        assert requirements_parser.matches_file("requirements.txt")
        assert requirements_parser.matches_file("requirements-dev.txt")
        assert requirements_parser.matches_file("requirements_test.txt")
        assert requirements_parser.matches_file("constraints.txt")
        assert requirements_parser.matches_file("path/to/requirements.txt")

    def test_does_not_match_other_files(self, requirements_parser: RequirementsTxtParser) -> None:
        assert not requirements_parser.matches_file("setup.py")
        assert not requirements_parser.matches_file("pyproject.toml")
        assert not requirements_parser.matches_file("Cargo.toml")

    def test_parse_version_bumps(
        self, requirements_parser: RequirementsTxtParser, requirements_txt_patch: str
    ) -> None:
        transitions = requirements_parser.parse(requirements_txt_patch, "requirements.txt")

        assert len(transitions) == 2

        # requests: 2.28.0 → 2.31.0
        req_t = next(t for t in transitions if t.package_name == "requests")
        assert req_t.old_version == "2.28.0"
        assert req_t.new_version == "2.31.0"
        assert req_t.ecosystem == Ecosystem.PYTHON
        assert req_t.source_file == "requirements.txt"

        # numpy: 1.21.0 → 1.24.0
        np_t = next(t for t in transitions if t.package_name == "numpy")
        assert np_t.old_version == "1.21.0"
        assert np_t.new_version == "1.24.0"

    def test_parse_new_dependency(self, requirements_parser: RequirementsTxtParser) -> None:
        patch = "+httpx>=0.24.0\n+pydantic>=2.0\n"
        transitions = requirements_parser.parse(patch, "requirements.txt")
        assert len(transitions) == 2
        httpx_t = next(t for t in transitions if t.package_name == "httpx")
        assert httpx_t.old_version is None
        assert httpx_t.new_version == "0.24.0"

    def test_parse_removed_dependency(self, requirements_parser: RequirementsTxtParser) -> None:
        patch = "-old-package==1.0.0\n"
        transitions = requirements_parser.parse(patch, "requirements.txt")
        assert len(transitions) == 1
        assert transitions[0].package_name == "old-package"
        assert transitions[0].old_version == "1.0.0"
        assert transitions[0].new_version is None

    def test_parse_ignores_comments_and_flags(
        self, requirements_parser: RequirementsTxtParser
    ) -> None:
        patch = "+# this is a comment\n+-r other.txt\n+--extra-index-url foo\n"
        transitions = requirements_parser.parse(patch, "requirements.txt")
        assert len(transitions) == 0

    def test_parse_no_version_change(self, requirements_parser: RequirementsTxtParser) -> None:
        patch = "-requests==2.28.0\n+requests==2.28.0\n"
        transitions = requirements_parser.parse(patch, "requirements.txt")
        assert len(transitions) == 0

    def test_parse_tilde_equals(self, requirements_parser: RequirementsTxtParser) -> None:
        patch = "-flask~=2.0.0\n+flask~=2.3.0\n"
        transitions = requirements_parser.parse(patch, "requirements.txt")
        assert len(transitions) == 1
        assert transitions[0].old_version == "2.0.0"
        assert transitions[0].new_version == "2.3.0"


class TestPyprojectTomlParser:
    """Tests for PyprojectTomlParser."""

    def test_matches_pyproject_toml(self, pyproject_parser: PyprojectTomlParser) -> None:
        assert pyproject_parser.matches_file("pyproject.toml")
        assert pyproject_parser.matches_file("path/to/pyproject.toml")

    def test_does_not_match_other_files(self, pyproject_parser: PyprojectTomlParser) -> None:
        assert not pyproject_parser.matches_file("requirements.txt")
        assert not pyproject_parser.matches_file("setup.py")

    def test_parse_version_bumps(
        self, pyproject_parser: PyprojectTomlParser, pyproject_toml_patch: str
    ) -> None:
        transitions = pyproject_parser.parse(pyproject_toml_patch, "pyproject.toml")

        assert len(transitions) == 2

        req_t = next(t for t in transitions if t.package_name == "requests")
        assert req_t.old_version == "2.28.0"
        assert req_t.new_version == "2.31.0"

        httpx_t = next(t for t in transitions if t.package_name == "httpx")
        assert httpx_t.old_version == "0.24.0"
        assert httpx_t.new_version == "0.27.0"


class TestSetupPyParser:
    """Tests for SetupPyParser — including real Spark PR #29686."""

    def test_matches_setup_py(self, setup_py_parser: SetupPyParser) -> None:
        assert setup_py_parser.matches_file("setup.py")
        assert setup_py_parser.matches_file("setup.cfg")
        assert setup_py_parser.matches_file("python/setup.py")

    def test_does_not_match_other_files(self, setup_py_parser: SetupPyParser) -> None:
        assert not setup_py_parser.matches_file("requirements.txt")
        assert not setup_py_parser.matches_file("pyproject.toml")

    def test_parse_spark_pr_29686(
        self, setup_py_parser: SetupPyParser, spark_setup_py_patch: str
    ) -> None:
        """Test with real Apache Spark PR #29686: pyarrow 0.15.1 → 1.0.0."""
        transitions = setup_py_parser.parse(spark_setup_py_patch, "python/setup.py")

        assert len(transitions) == 1
        t = transitions[0]
        assert t.package_name == "pyarrow"
        assert t.old_version == "0.15.1"
        assert t.new_version == "1.0.0"
        assert t.ecosystem == Ecosystem.PYTHON
        assert t.source_file == "python/setup.py"

    def test_parse_install_requires(self, setup_py_parser: SetupPyParser) -> None:
        patch = '-    "requests>=2.28.0",\n+    "requests>=2.31.0",\n'
        transitions = setup_py_parser.parse(patch, "setup.py")
        assert len(transitions) == 1
        assert transitions[0].package_name == "requests"
        assert transitions[0].old_version == "2.28.0"
        assert transitions[0].new_version == "2.31.0"

    def test_parse_version_variable_underscore_normalization(
        self, setup_py_parser: SetupPyParser
    ) -> None:
        """Package names with underscores get normalized to hyphens."""
        patch = (
            '-_minimum_my_package_version = "1.0.0"\n'
            '+_minimum_my_package_version = "2.0.0"\n'
        )
        transitions = setup_py_parser.parse(patch, "setup.py")
        assert len(transitions) == 1
        assert transitions[0].package_name == "my-package"
