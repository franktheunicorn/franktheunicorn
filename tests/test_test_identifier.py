"""Tests for three-source test identification (§9.1)."""

from __future__ import annotations

from franktheunicorn.worker.test_identifier import (
    identify_test_scope,
    identify_tests_from_description,
    identify_tests_from_diff,
    identify_tests_from_template,
)


class TestIdentifyFromDiff:
    def test_finds_test_files(self) -> None:
        files = ["src/app.py", "tests/test_app.py", "README.md"]
        assert identify_tests_from_diff(files) == ["tests/test_app.py"]

    def test_finds_spec_files(self) -> None:
        files = ["spec/app_spec.rb", "lib/app.rb"]
        assert identify_tests_from_diff(files) == ["spec/app_spec.rb"]

    def test_no_test_files(self) -> None:
        assert identify_tests_from_diff(["src/main.py"]) == []

    def test_multiple_test_files(self) -> None:
        files = ["test_a.py", "tests/test_b.py", "src/main.py"]
        result = identify_tests_from_diff(files)
        assert len(result) == 2


class TestIdentifyFromDescription:
    def test_finds_test_file_reference(self) -> None:
        body = "Tests: tests/test_app.py"
        result = identify_tests_from_description(body)
        assert "tests/test_app.py" in result

    def test_no_tests(self) -> None:
        assert identify_tests_from_description("Fixed a bug.") == []


class TestIdentifyFromTemplate:
    def test_finds_in_test_plan_section(self) -> None:
        body = "## Test Plan\n- [x] tests/test_foo.py\n- [x] tests/test_bar.py\n## Other"
        result = identify_tests_from_template(body)
        assert "tests/test_foo.py" in result
        assert "tests/test_bar.py" in result

    def test_no_test_section(self) -> None:
        assert identify_tests_from_template("No test plan here.") == []


class TestIdentifyTestScope:
    def test_combines_all_sources(self) -> None:
        files = ["tests/test_a.py", "src/main.py"]
        body = "Tests: tests/test_b.py\n## Test Plan\n- tests/test_c.py"
        result = identify_test_scope(files, body)
        assert "tests/test_a.py" in result
        assert "tests/test_b.py" in result
        assert "tests/test_c.py" in result

    def test_deduplicates(self) -> None:
        files = ["tests/test_a.py"]
        body = "Tests: tests/test_a.py"
        result = identify_test_scope(files, body)
        assert result.count("tests/test_a.py") == 1
