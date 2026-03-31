"""Tests for the local git blame fetcher (v1.25)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from franktheunicorn.scoring.blame_fetcher import (
    MAX_BLAME_FILES,
    _classify_authors,
    _is_code_file,
    _parse_diff_changed_lines,
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
    """Create a temporary git repo with commits from two authors.

    alice writes main.py (3 lines) and utils.py (3 lines).
    bob modifies line 2 of main.py.

    After both commits:
    - main.py line 1,3 blamed to alice, line 2 blamed to bob
    - utils.py all lines blamed to alice
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "alice@example.com")
    _git(repo, "config", "user.name", "alice")

    # First commit by alice — create both files
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("line1\nline2\nline3\n")
    (repo / "src" / "utils.py").write_text("util1\nutil2\nutil3\n")
    (repo / "README.md").write_text("# Project\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit by alice")

    # Tag alice's commit so we can diff against it
    _git(repo, "tag", "alice-base")

    # Second commit by bob — modify line 2 of main.py only
    _git(repo, "config", "user.email", "bob@example.com")
    _git(repo, "config", "user.name", "bob")
    (repo / "src" / "main.py").write_text("line1\nmodified-by-bob\nline3\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Bob modifies line 2")

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
        sha = "a" * 40
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


class TestParseDiffChangedLines:
    def test_single_hunk(self) -> None:
        diff = "@@ -10,3 +10,4 @@\n-old\n+new\n+added\n"
        result = _parse_diff_changed_lines(diff)
        assert result == {10, 11, 12}

    def test_multiple_hunks(self) -> None:
        diff = "@@ -5,2 +5,2 @@\n-a\n+b\n@@ -20,1 +20,1 @@\n-c\n+d\n"
        result = _parse_diff_changed_lines(diff)
        assert 5 in result
        assert 6 in result
        assert 20 in result

    def test_single_line_hunk(self) -> None:
        diff = "@@ -7 +7 @@\n-old\n+new\n"
        result = _parse_diff_changed_lines(diff)
        assert result == {7}

    def test_pure_insertion_no_old_lines(self) -> None:
        # -U0 format: count=0 means pure insertion, no old lines changed
        diff = "@@ -5,0 +6,3 @@\n+new1\n+new2\n+new3\n"
        result = _parse_diff_changed_lines(diff)
        assert result == set()  # no old lines were modified

    def test_no_hunks(self) -> None:
        assert _parse_diff_changed_lines("no diff here") == set()


class TestClassifyAuthors:
    def test_direct_and_near_separation(self) -> None:
        blame = {
            1: "alice",
            2: "alice",
            3: "bob",  # changed line
            4: "charlie",
            5: "alice",
            10: "dave",  # far away
        }
        changed_lines = {3}
        direct, near_only = _classify_authors(blame, changed_lines, near_window=2)
        # bob authored the changed line → direct
        assert direct == {"bob"}
        # alice (lines 1,2,5) and charlie (line 4) are within 2 lines → near
        # but alice is NOT in direct, so she's near_only
        assert "alice" in near_only
        assert "charlie" in near_only
        # dave at line 10 is far away → neither
        assert "dave" not in near_only
        assert "dave" not in direct

    def test_author_in_both_direct_and_near_is_only_direct(self) -> None:
        """If an author has lines in both changed and near, they're direct only."""
        blame = {
            1: "alice",  # near line 3
            3: "alice",  # changed line
            5: "bob",  # near line 3
        }
        changed_lines = {3}
        direct, near_only = _classify_authors(blame, changed_lines, near_window=2)
        assert direct == {"alice"}
        assert near_only == {"bob"}

    def test_empty_changed_lines(self) -> None:
        blame = {1: "alice", 2: "bob"}
        direct, near_only = _classify_authors(blame, set())
        assert direct == set()
        assert near_only == set()


class TestFetchBlameForFile:
    def test_blame_existing_file(self, git_repo: Path) -> None:
        result = fetch_blame_for_file(git_repo, "src/main.py", "HEAD")
        assert result is not None
        assert len(result) == 3
        # Line 2 was modified by bob, lines 1,3 by alice
        assert result[1] == "alice"
        assert result[2] == "bob"
        assert result[3] == "alice"

    def test_blame_unmodified_file(self, git_repo: Path) -> None:
        result = fetch_blame_for_file(git_repo, "src/utils.py", "HEAD")
        assert result is not None
        assert all(author == "alice" for author in result.values())

    def test_blame_nonexistent_file(self, git_repo: Path) -> None:
        result = fetch_blame_for_file(git_repo, "nonexistent.py", "HEAD")
        assert result is None

    def test_blame_not_a_repo(self, tmp_path: Path) -> None:
        result = fetch_blame_for_file(tmp_path, "anything.py", "HEAD")
        assert result is None


class TestFetchBlameForFiles:
    def test_classifies_direct_vs_near_with_two_ref_diff(self, git_repo: Path) -> None:
        """Core test: two-ref diff (base..head) correctly identifies changed lines."""
        # Diff from alice-base..HEAD: bob changed line 2 of main.py.
        # Blame on alice-base shows alice authored all 3 lines.
        # The diff should show line 2 changed → alice is in "authors" (she
        # authored the base line being modified).
        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py"],
            base_ref="alice-base",
            head_ref="HEAD",
        )
        assert len(results) == 1
        entry = results[0]
        assert entry["file_path"] == "src/main.py"
        authors = entry["authors"]
        assert isinstance(authors, list)
        assert "alice" in authors

    def test_fallback_single_ref_diff(self, git_repo: Path) -> None:
        """When head_ref is None, falls back to diff against working tree."""
        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py"],
            base_ref="alice-base",
            head_ref=None,
        )
        # Should still work (repo working tree is at HEAD which is bob's commit)
        assert len(results) == 1

    def test_skips_non_code_files(self, git_repo: Path) -> None:
        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py", "README.md"],
            base_ref="alice-base",
            head_ref="HEAD",
        )
        file_paths = [r["file_path"] for r in results]
        assert "README.md" not in file_paths

    def test_caps_at_max_files(self, git_repo: Path) -> None:
        many_files = [f"file_{i}.py" for i in range(MAX_BLAME_FILES + 10)]
        results = fetch_blame_for_files(git_repo, many_files, base_ref="HEAD")
        assert len(results) <= MAX_BLAME_FILES

    def test_empty_file_list(self, git_repo: Path) -> None:
        results = fetch_blame_for_files(git_repo, [])
        assert results == []

    def test_nonexistent_files_skipped(self, git_repo: Path) -> None:
        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py", "nonexistent.py"],
            base_ref="alice-base",
            head_ref="HEAD",
        )
        file_paths = [r["file_path"] for r in results]
        assert "nonexistent.py" not in file_paths

    def test_near_authors_separate_from_direct(self, git_repo: Path) -> None:
        """near_authors should not contain anyone already in authors."""
        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py"],
            base_ref="alice-base",
            head_ref="HEAD",
        )
        if results:
            entry = results[0]
            direct = set(entry["authors"])  # type: ignore[arg-type]
            near = set(entry["near_authors"])  # type: ignore[arg-type]
            # No overlap between direct and near
            assert direct & near == set()


class TestEndToEndBlameScoring:
    """Integration: blame fetcher output feeds into the scorer correctly."""

    def test_scorer_uses_blame_data(self, git_repo: Path) -> None:
        from franktheunicorn.scoring.blame import score_touches_operator_code

        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py"],
            base_ref="alice-base",
            head_ref="HEAD",
        )
        assert len(results) > 0

        # alice authored the changed lines → should get full credit
        score = score_touches_operator_code(results, "alice")
        assert score is not None
        assert score > 0

    def test_scorer_no_credit_for_uninvolved(self, git_repo: Path) -> None:
        from franktheunicorn.scoring.blame import score_touches_operator_code

        results = fetch_blame_for_files(
            git_repo,
            ["src/main.py"],
            base_ref="alice-base",
            head_ref="HEAD",
        )
        # "charlie" never touched any file → no credit
        score = score_touches_operator_code(results, "charlie")
        assert score is None
