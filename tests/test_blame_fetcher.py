"""Tests for the local git blame fetcher (v1.25)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from franktheunicorn.scoring.blame_fetcher import (
    MAX_BLAME_FILES,
    BlameEntry,
    _is_code_file,
    _parse_porcelain_blame,
    fetch_blame_for_file,
    fetch_blame_for_files,
)


def _git(repo: Path, *args: str) -> None:
    """Run a git command in the given repo with signing disabled."""
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with commits from two authors."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "alice@example.com")
    _git(repo, "config", "user.name", "alice")

    # First commit by alice
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("# main\nprint('hello')\nprint('world')\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")

    # Second commit by bob
    _git(repo, "config", "user.email", "bob@example.com")
    _git(repo, "config", "user.name", "bob")
    (repo / "src" / "utils.py").write_text("# utils\ndef helper():\n    return 42\n")
    (repo / "README.md").write_text("# Project\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Add utils")

    return repo


class TestIsCodeFile:
    def test_python_is_code(self) -> None:
        assert _is_code_file("src/main.py") is True

    def test_java_is_code(self) -> None:
        assert _is_code_file("src/Main.java") is True

    def test_markdown_is_not_code(self) -> None:
        assert _is_code_file("README.md") is False

    def test_yaml_is_not_code(self) -> None:
        assert _is_code_file("config.yaml") is False

    def test_json_is_not_code(self) -> None:
        assert _is_code_file("package.json") is False

    def test_lock_is_not_code(self) -> None:
        assert _is_code_file("poetry.lock") is False


class TestParsePorcelainBlame:
    def test_basic_parsing(self) -> None:
        # First occurrence of a commit includes full metadata.
        # Second occurrence only has the header line.
        sha = "a" * 40  # exactly 40 hex chars
        output = (
            f"{sha} 1 1 2\n"
            "author Alice\n"
            "author-mail <alice@example.com>\n"
            "author-time 1700000000\n"
            "author-tz +0000\n"
            "committer Alice\n"
            "committer-mail <alice@example.com>\n"
            "committer-time 1700000000\n"
            "committer-tz +0000\n"
            "summary Initial commit\n"
            "filename src/main.py\n"
            "\t# main\n"
            f"{sha} 2 2\n"
            "\tprint('hello')\n"
        )
        result = _parse_porcelain_blame(output)
        assert result[1] == "Alice"
        assert result[2] == "Alice"

    def test_multiple_authors(self) -> None:
        sha_a = "a" * 40
        sha_b = "b" * 40
        output = (
            f"{sha_a} 1 1 1\n"
            "author Alice\n"
            "filename file.py\n"
            "\tline1\n"
            f"{sha_b} 2 2 1\n"
            "author Bob\n"
            "filename file.py\n"
            "\tline2\n"
        )
        result = _parse_porcelain_blame(output)
        assert result[1] == "Alice"
        assert result[2] == "Bob"

    def test_empty_output(self) -> None:
        assert _parse_porcelain_blame("") == {}


class TestFetchBlameForFile:
    def test_blame_existing_file(self, git_repo: Path) -> None:
        result = fetch_blame_for_file(git_repo, "src/main.py", "HEAD")
        assert result is not None
        assert len(result) > 0
        # All lines should be authored by alice
        assert all(author == "alice" for author in result.values())

    def test_blame_file_by_bob(self, git_repo: Path) -> None:
        result = fetch_blame_for_file(git_repo, "src/utils.py", "HEAD")
        assert result is not None
        assert all(author == "bob" for author in result.values())

    def test_blame_nonexistent_file(self, git_repo: Path) -> None:
        result = fetch_blame_for_file(git_repo, "nonexistent.py", "HEAD")
        assert result is None

    def test_blame_not_a_repo(self, tmp_path: Path) -> None:
        result = fetch_blame_for_file(tmp_path, "anything.py", "HEAD")
        assert result is None


class TestFetchBlameForFiles:
    def test_returns_blame_for_code_files(self, git_repo: Path) -> None:
        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py", "src/utils.py"],
        )
        assert len(results) == 2
        main_entry = next(r for r in results if r["file_path"] == "src/main.py")
        assert "alice" in main_entry["authors"]
        utils_entry = next(r for r in results if r["file_path"] == "src/utils.py")
        assert "bob" in utils_entry["authors"]

    def test_skips_non_code_files(self, git_repo: Path) -> None:
        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py", "README.md"],
        )
        # README.md should be skipped (markdown)
        assert len(results) == 1
        assert results[0]["file_path"] == "src/main.py"

    def test_caps_at_max_files(self, git_repo: Path) -> None:
        many_files = [f"file_{i}.py" for i in range(MAX_BLAME_FILES + 10)]
        results = fetch_blame_for_files(git_repo, many_files)
        # All files are nonexistent, so results will be empty (blame fails),
        # but the function should not attempt more than MAX_BLAME_FILES.
        assert len(results) <= MAX_BLAME_FILES

    def test_empty_file_list(self, git_repo: Path) -> None:
        results = fetch_blame_for_files(git_repo, [])
        assert results == []

    def test_nonexistent_files_skipped(self, git_repo: Path) -> None:
        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py", "nonexistent.py"],
        )
        assert len(results) == 1
        assert results[0]["file_path"] == "src/main.py"
