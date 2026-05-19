"""Tests for the copy-pasta detection system."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import franktheunicorn.review.copypasta as copypasta_module
from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import PullRequest, ReviewDraft
from franktheunicorn.data_access.github.types import PRDiff, PRFileChange
from franktheunicorn.review.copypasta import (
    CopyPastaMatch,
    _check_llm,
    _check_symilar,
    _check_winnowing,
    _create_drafts,
    _read_repo_files,
    check_copypasta,
    extract_added_chunks,
)


def _check_git_available() -> bool:
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


_GIT_AVAILABLE = _check_git_available()
_skip_no_git = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not installed")

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def copypasta_config() -> ProjectConfig:
    return ProjectConfig(
        owner="apache",
        repo="spark",
        copypasta_enabled=True,
        copypasta_min_lines=4,
        copypasta_scan_extensions=[".py"],
    )


@pytest.fixture
def disabled_config() -> ProjectConfig:
    return ProjectConfig(
        owner="apache",
        repo="spark",
        copypasta_enabled=False,
    )


@pytest.fixture
def simple_patch() -> str:
    """A unified diff patch with one hunk of added lines."""
    return (
        "@@ -0,0 +1,6 @@\n"
        "+def calculate_total(items):\n"
        "+    total = 0\n"
        "+    for item in items:\n"
        "+        total += item.price * item.quantity\n"
        "+    return total\n"
        "+\n"
    )


@pytest.fixture
def multi_hunk_patch() -> str:
    """A patch with multiple hunks."""
    return (
        "@@ -10,2 +10,6 @@\n"
        " existing line\n"
        "+def first_func():\n"
        "+    a = 1\n"
        "+    b = 2\n"
        "+    return a + b\n"
        " another existing line\n"
        "@@ -30,2 +34,6 @@\n"
        " context line\n"
        "+def second_func():\n"
        "+    x = 10\n"
        "+    y = 20\n"
        "+    return x * y\n"
        " trailing context\n"
    )


@pytest.fixture
def diff_with_duplication(simple_patch: str) -> PRDiff:
    return PRDiff(
        pr_number=42,
        raw_diff="",
        files=(
            PRFileChange(
                filename="src/utils.py",
                status="added",
                additions=6,
                deletions=0,
                patch=simple_patch,
            ),
        ),
    )


@pytest.fixture
def repo_with_duplicate(tmp_path: Path) -> Path:
    """Create a temporary repo directory with a file containing duplicate code."""
    if not _GIT_AVAILABLE:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()

    # Initialize a git repo so git ls-files works
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)

    # Create a file with the same code that will appear in the PR
    src_dir = repo / "src"
    src_dir.mkdir()
    existing_file = src_dir / "existing.py"
    existing_file.write_text(
        "# Existing utility module\n"
        "\n"
        "def calculate_total(items):\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        total += item.price * item.quantity\n"
        "    return total\n"
        "\n"
        "def other_function():\n"
        "    pass\n"
    )

    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo,
        capture_output=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", ""),
        },
    )
    return repo


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    """Create an empty git repo."""
    if not _GIT_AVAILABLE:
        pytest.skip("git not installed")
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    return repo


# -- Test extract_added_chunks -----------------------------------------------


class TestExtractAddedChunks:
    def test_simple_patch(self, simple_patch: str) -> None:
        diff = PRDiff(
            pr_number=1,
            files=(PRFileChange(filename="test.py", status="added", patch=simple_patch),),
        )
        chunks = extract_added_chunks(diff, min_lines=4)
        assert len(chunks) == 1
        assert chunks[0].file_path == "test.py"
        assert chunks[0].start_line == 1
        assert len(chunks[0].lines) == 6
        assert chunks[0].lines[0] == "def calculate_total(items):"

    def test_skips_short_chunks(self, simple_patch: str) -> None:
        diff = PRDiff(
            pr_number=1,
            files=(PRFileChange(filename="test.py", status="added", patch=simple_patch),),
        )
        chunks = extract_added_chunks(diff, min_lines=10)
        assert len(chunks) == 0

    def test_multi_hunk(self, multi_hunk_patch: str) -> None:
        diff = PRDiff(
            pr_number=1,
            files=(PRFileChange(filename="test.py", status="modified", patch=multi_hunk_patch),),
        )
        chunks = extract_added_chunks(diff, min_lines=4)
        assert len(chunks) == 2
        assert chunks[0].start_line == 11
        assert chunks[1].start_line == 35

    def test_skips_removed_files(self) -> None:
        diff = PRDiff(
            pr_number=1,
            files=(
                PRFileChange(
                    filename="deleted.py",
                    status="removed",
                    patch="@@ -1,3 +0,0 @@\n-line1\n-line2\n-line3\n",
                ),
            ),
        )
        chunks = extract_added_chunks(diff, min_lines=1)
        assert len(chunks) == 0

    def test_empty_patch(self) -> None:
        diff = PRDiff(
            pr_number=1,
            files=(PRFileChange(filename="test.py", status="modified", patch=""),),
        )
        chunks = extract_added_chunks(diff, min_lines=1)
        assert len(chunks) == 0

    def test_context_lines_break_chunks(self) -> None:
        """Context lines should split added code into separate chunks."""
        patch = (
            "@@ -1,1 +1,9 @@\n"
            "+line1\n"
            "+line2\n"
            "+line3\n"
            "+line4\n"
            " context\n"
            "+line5\n"
            "+line6\n"
            "+line7\n"
            "+line8\n"
        )
        diff = PRDiff(
            pr_number=1,
            files=(PRFileChange(filename="test.py", status="modified", patch=patch),),
        )
        chunks = extract_added_chunks(diff, min_lines=4)
        assert len(chunks) == 2


# -- Test tier 1a: symilar ---------------------------------------------------


class TestCheckSymilar:
    def test_detects_exact_duplicate(self) -> None:
        code = (
            "def calculate_total(items):",
            "    total = 0",
            "    for item in items:",
            "        total += item.price * item.quantity",
            "    return total",
        )
        from franktheunicorn.review.copypasta import CodeChunk

        chunks = [CodeChunk(file_path="new.py", start_line=1, lines=code)]
        repo_files = {
            "existing.py": "\n".join(code) + "\n",
        }
        matches = _check_symilar(chunks, repo_files, min_lines=4)
        assert len(matches) >= 1
        assert matches[0].source_file == "existing.py"
        assert matches[0].tier == "symilar"

    def test_no_match_for_unique_code(self) -> None:
        from franktheunicorn.review.copypasta import CodeChunk

        chunks = [
            CodeChunk(
                file_path="new.py",
                start_line=1,
                lines=(
                    "def unique_function():",
                    "    result = compute_something()",
                    "    transform(result)",
                    "    save_to_database(result)",
                    "    return result",
                ),
            )
        ]
        repo_files = {
            "other.py": (
                "def completely_different():\n"
                "    x = read_config()\n"
                "    validate(x)\n"
                "    process(x)\n"
                "    return x\n"
            ),
        }
        matches = _check_symilar(chunks, repo_files, min_lines=4)
        assert len(matches) == 0

    def test_match_detected_regardless_of_iteration_order(self) -> None:
        """The elif branch fires when the repo lineset appears before the PR chunk in Symilar."""
        code = (
            "def calculate_total(items):",
            "    total = 0",
            "    for item in items:",
            "        total += item.price * item.quantity",
            "    return total",
        )
        from franktheunicorn.review.copypasta import CodeChunk

        # Feed repo file first so Symilar appends it before the PR chunk,
        # triggering the `elif ls2.name in chunk_names` branch.
        chunks = [CodeChunk(file_path="new.py", start_line=1, lines=code)]
        repo_files = {"existing.py": "\n".join(code) + "\n"}

        sym = __import__("pylint.checkers.symilar", fromlist=["Symilar"]).Symilar(
            min_lines=4,
            ignore_comments=True,
            ignore_docstrings=True,
            ignore_imports=True,
        )
        from io import StringIO

        # Append repo file FIRST, then the PR chunk — reverses the default order
        sym.append_stream("existing.py", StringIO("\n".join(code) + "\n"))
        sym.append_stream("pr_chunk:new.py:1", StringIO("\n".join(code) + "\n"))
        sym.run()

        # Confirm Symilar actually reversed the lineset order (repo is linesets[0])
        assert sym.linesets[0].name == "existing.py"

        # Now run through _check_symilar using the same reversed ordering
        matches = _check_symilar(chunks, repo_files, min_lines=4)
        assert len(matches) >= 1

    def test_ignores_leading_whitespace_differences(self) -> None:
        """Symilar normalizes leading whitespace (indentation style)."""
        from franktheunicorn.review.copypasta import CodeChunk

        chunks = [
            CodeChunk(
                file_path="new.py",
                start_line=1,
                lines=(
                    "def hello():",
                    "  x = 1",
                    "  y = 2",
                    "  z = x + y",
                    "  return z",
                ),
            )
        ]
        repo_files = {
            "existing.py": ("def hello():\n    x = 1\n    y = 2\n    z = x + y\n    return z\n"),
        }
        matches = _check_symilar(chunks, repo_files, min_lines=4)
        assert len(matches) >= 1

    def test_unparseable_chunk_does_not_crash(self) -> None:
        """Non-Python content (YAML, malformed string literals) must not propagate
        AstroidSyntaxError — the chunk is skipped and an empty result is returned."""
        from franktheunicorn.review.copypasta import CodeChunk

        # A docstring-like line with an unmatched backtick — triggers the exact
        # error seen in production: "unterminated string literal"
        bad_lines = (
            "* top StructType of a file-source metadata attribute (e.g. `_metadata`'s",
            "  corresponding field type) rather than the root type of the schema",
            "  definition. This is intentional and not a bug in the schema parser.",
            "  See the schema validation docs for more details about this behaviour.",
        )
        chunks = [CodeChunk(file_path="CHANGES.md", start_line=1, lines=bad_lines)]
        repo_files = {"existing.py": "def foo():\n    pass\n"}
        # Must return without raising
        matches = _check_symilar(chunks, repo_files, min_lines=4)
        assert isinstance(matches, list)

    def test_unparseable_repo_file_does_not_crash(self) -> None:
        """A repo file that fails AST parsing is silently skipped."""
        from franktheunicorn.review.copypasta import CodeChunk

        valid_code = (
            "def hello():",
            "    x = 1",
            "    y = 2",
            "    return x + y",
        )
        chunks = [CodeChunk(file_path="new.py", start_line=1, lines=valid_code)]
        repo_files = {
            "valid.py": "\n".join(valid_code) + "\n",
            "bad.md": "# `unterminated\nsome text\nmore text\neven more text\n",
        }
        # Must return without raising; valid.py match may still be found
        matches = _check_symilar(chunks, repo_files, min_lines=4)
        assert isinstance(matches, list)


# -- Test tier 1b: winnowing -------------------------------------------------


class TestCheckWinnowing:
    def test_detects_fingerprint_overlap(self) -> None:
        from franktheunicorn.review.copypasta import CodeChunk

        code_lines = (
            "def calculate_total(items):",
            "    total = 0",
            "    for item in items:",
            "        total += item.price * item.quantity",
            "    return total",
        )
        chunks = [CodeChunk(file_path="new.py", start_line=1, lines=code_lines)]
        repo_files = {
            "existing.py": "\n".join(code_lines) + "\n",
        }
        matches = _check_winnowing(chunks, repo_files)
        assert len(matches) >= 1
        assert matches[0].tier == "winnowing"

    def test_no_match_for_different_code(self) -> None:
        from franktheunicorn.review.copypasta import CodeChunk

        chunks = [
            CodeChunk(
                file_path="new.py",
                start_line=1,
                lines=(
                    "class DatabaseManager:",
                    "    def __init__(self, connection_string):",
                    "        self.conn = connect(connection_string)",
                    "        self.cursor = self.conn.cursor()",
                    "        self.ready = True",
                ),
            )
        ]
        repo_files = {
            "other.py": (
                "def parse_arguments():\n"
                "    parser = argparse.ArgumentParser()\n"
                "    parser.add_argument('--verbose')\n"
                "    parser.add_argument('--output')\n"
                "    return parser.parse_args()\n"
            ),
        }
        matches = _check_winnowing(chunks, repo_files)
        assert len(matches) == 0

    def test_skips_short_chunks(self) -> None:
        """Chunks shorter than the k-gram size should be skipped gracefully."""
        from franktheunicorn.review.copypasta import CodeChunk

        chunks = [CodeChunk(file_path="new.py", start_line=1, lines=("x = 1",))]
        repo_files = {"existing.py": "x = 1\n"}
        matches = _check_winnowing(chunks, repo_files)
        assert len(matches) == 0

    def test_skips_empty_repo_file_content(self) -> None:
        from franktheunicorn.review.copypasta import CodeChunk

        chunks = [
            CodeChunk(
                file_path="new.py",
                start_line=1,
                lines=(
                    "def calculate_total(items):",
                    "    total = 0",
                    "    for item in items:",
                    "        total += item.price * item.quantity",
                    "    return total",
                ),
            )
        ]
        matches = _check_winnowing(chunks, {"empty.py": "   \n"})
        assert matches == []

    def test_handles_fingerprint_exception_for_repo_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from franktheunicorn.review.copypasta import CodeChunk

        original_cls = copypasta_module.CodeFingerprint

        call_count = 0

        def raising_fp(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("bad repo file")
            return original_cls(*args, **kwargs)

        monkeypatch.setattr(copypasta_module, "CodeFingerprint", raising_fp)

        chunks = [CodeChunk(file_path="new.py", start_line=1, lines=("x = 1",) * 5)]
        matches = _check_winnowing(chunks, {"repo.py": "x = 1\n" * 5})
        assert matches == []

    def test_handles_fingerprint_exception_for_chunk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from franktheunicorn.review.copypasta import CodeChunk

        original_cls = copypasta_module.CodeFingerprint

        call_count = 0

        def raising_fp(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("bad chunk")
            return original_cls(*args, **kwargs)

        monkeypatch.setattr(copypasta_module, "CodeFingerprint", raising_fp)

        code_lines = tuple(["x = 1"] * 5)
        chunks = [CodeChunk(file_path="new.py", start_line=1, lines=code_lines)]
        matches = _check_winnowing(chunks, {"repo.py": "x = 1\n" * 5})
        assert matches == []

    def test_handles_compare_files_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from franktheunicorn.review.copypasta import CodeChunk

        def raise_compare(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("compare exploded")

        monkeypatch.setattr(copypasta_module, "compare_files", raise_compare)

        code_lines = tuple(["x = 1"] * 5)
        chunks = [CodeChunk(file_path="new.py", start_line=1, lines=code_lines)]
        matches = _check_winnowing(chunks, {"repo.py": "x = 1\n" * 5})
        assert matches == []


# -- Test tier 2: LLM stub --------------------------------------------------


class TestCheckLlm:
    def test_stub_returns_empty(self) -> None:
        from franktheunicorn.review.copypasta import CodeChunk

        chunks = [
            CodeChunk(file_path="new.py", start_line=1, lines=("line1", "line2", "line3", "line4"))
        ]
        matches = _check_llm(chunks, {"file.py": "content"})
        assert matches == []


# -- Test _read_repo_files ---------------------------------------------------


class TestReadRepoFiles:
    def _make_run(self, stdout: str) -> Any:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

        return fake_run

    def test_returns_files_filtered_by_extension(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("def foo(): pass\n")
        monkeypatch.setattr(
            copypasta_module.subprocess, "run", self._make_run("src/foo.py\nsrc/bar.js\n")
        )
        result = _read_repo_files(tmp_path, extensions=[".py"], ignore_paths=[], exclude_files=None)
        assert "src/foo.py" in result
        assert "src/bar.js" not in result

    def test_respects_ignore_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "foo.py").write_text("# vendor\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "bar.py").write_text("# src\n")
        monkeypatch.setattr(
            copypasta_module.subprocess, "run", self._make_run("vendor/foo.py\nsrc/bar.py\n")
        )
        result = _read_repo_files(
            tmp_path, extensions=[".py"], ignore_paths=["vendor/"], exclude_files=None
        )
        assert "vendor/foo.py" not in result
        assert "src/bar.py" in result

    def test_respects_exclude_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "modified.py").write_text("# modified\n")
        (tmp_path / "src" / "other.py").write_text("# other\n")
        monkeypatch.setattr(
            copypasta_module.subprocess,
            "run",
            self._make_run("src/modified.py\nsrc/other.py\n"),
        )
        result = _read_repo_files(
            tmp_path,
            extensions=[".py"],
            ignore_paths=[],
            exclude_files={"src/modified.py"},
        )
        assert "src/modified.py" not in result
        assert "src/other.py" in result

    def test_empty_output_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(copypasta_module.subprocess, "run", self._make_run(""))
        result = _read_repo_files(tmp_path, extensions=[".py"], ignore_paths=[], exclude_files=None)
        assert result == {}

    def test_git_not_found_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_fnf(*args: Any, **kwargs: Any) -> None:
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(copypasta_module.subprocess, "run", raise_fnf)
        result = _read_repo_files(tmp_path, extensions=[".py"], ignore_paths=[], exclude_files=None)
        assert result == {}

    def test_timeout_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_timeout(*args: Any, **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd=["git", "ls-files"], timeout=30)

        monkeypatch.setattr(copypasta_module.subprocess, "run", raise_timeout)
        result = _read_repo_files(tmp_path, extensions=[".py"], ignore_paths=[], exclude_files=None)
        assert result == {}

    def test_called_process_error_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_cpe(*args: Any, **kwargs: Any) -> None:
            raise subprocess.CalledProcessError(returncode=128, cmd=["git", "ls-files"])

        monkeypatch.setattr(copypasta_module.subprocess, "run", raise_cpe)
        result = _read_repo_files(tmp_path, extensions=[".py"], ignore_paths=[], exclude_files=None)
        assert result == {}

    def test_os_error_on_read_skips_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # File is listed by git but does not exist on disk → OSError on read_text
        monkeypatch.setattr(
            copypasta_module.subprocess, "run", self._make_run("src/unreadable.py\n")
        )
        result = _read_repo_files(tmp_path, extensions=[".py"], ignore_paths=[], exclude_files=None)
        assert result == {}


# -- Test config validation --------------------------------------------------


class TestCopypastaConfig:
    def test_default_disabled(self) -> None:
        config = ProjectConfig(owner="test", repo="test")
        assert config.copypasta_enabled is False
        assert config.copypasta_min_lines == 4

    def test_min_lines_validation(self) -> None:
        with pytest.raises(ValueError, match="copypasta_min_lines must be at least 2"):
            ProjectConfig(owner="test", repo="test", copypasta_min_lines=1)

    def test_min_lines_accepts_2(self) -> None:
        config = ProjectConfig(owner="test", repo="test", copypasta_min_lines=2)
        assert config.copypasta_min_lines == 2


# -- Test create_drafts ------------------------------------------------------


@pytest.mark.django_db
class TestCreateDrafts:
    def test_creates_review_drafts(self, db_pr: PullRequest) -> None:
        matches = [
            CopyPastaMatch(
                source_file="existing.py",
                source_start_line=5,
                source_end_line=10,
                new_file="new.py",
                new_start_line=1,
                num_lines=5,
                tier="symilar",
            ),
        ]
        drafts = _create_drafts(db_pr, matches)
        assert len(drafts) == 1
        assert drafts[0].file_path == "new.py"
        assert drafts[0].line_number == 1
        assert "existing.py" in drafts[0].comment_body
        assert drafts[0].confidence == 0.85
        assert drafts[0].status == "pending"

    def test_deduplicates_by_location(self, db_pr: PullRequest) -> None:
        matches = [
            CopyPastaMatch(
                source_file="a.py",
                source_start_line=1,
                source_end_line=5,
                new_file="new.py",
                new_start_line=1,
                num_lines=5,
                tier="symilar",
            ),
            CopyPastaMatch(
                source_file="b.py",
                source_start_line=10,
                source_end_line=15,
                new_file="new.py",
                new_start_line=1,
                num_lines=5,
                tier="winnowing",
            ),
        ]
        drafts = _create_drafts(db_pr, matches)
        assert len(drafts) == 1  # second match deduped

    def test_winnowing_tier_confidence(self, db_pr: PullRequest) -> None:
        matches = [
            CopyPastaMatch(
                source_file="existing.py",
                source_start_line=1,
                source_end_line=5,
                new_file="new.py",
                new_start_line=1,
                num_lines=5,
                tier="winnowing",
            ),
        ]
        drafts = _create_drafts(db_pr, matches)
        assert drafts[0].confidence == 0.85

    def test_same_line_location(self, db_pr: PullRequest) -> None:
        """When source_start_line == source_end_line the body shows only the file name."""
        matches = [
            CopyPastaMatch(
                source_file="utils.py",
                source_start_line=7,
                source_end_line=7,
                new_file="new.py",
                new_start_line=3,
                num_lines=1,
                tier="symilar",
            ),
        ]
        drafts = _create_drafts(db_pr, matches)
        assert len(drafts) == 1
        body = drafts[0].comment_body
        assert "`utils.py`" in body
        # The location string should be just the filename, not "lines N-N"
        assert "(lines" not in body


# -- Test end-to-end check_copypasta ----------------------------------------


@pytest.mark.django_db
class TestCheckCopypasta:
    def test_disabled_returns_empty(
        self,
        db_pr: PullRequest,
        diff_with_duplication: PRDiff,
        disabled_config: ProjectConfig,
        empty_repo: Path,
    ) -> None:
        result = check_copypasta(db_pr, diff_with_duplication, disabled_config, empty_repo)
        assert result == []

    def test_no_chunks_returns_empty(
        self,
        db_pr: PullRequest,
        copypasta_config: ProjectConfig,
        empty_repo: Path,
    ) -> None:
        empty_diff = PRDiff(pr_number=42, files=())
        result = check_copypasta(db_pr, empty_diff, copypasta_config, empty_repo)
        assert result == []

    @pytest.mark.integration
    @_skip_no_git
    def test_end_to_end_with_duplicate(
        self,
        db_pr: PullRequest,
        diff_with_duplication: PRDiff,
        copypasta_config: ProjectConfig,
        repo_with_duplicate: Path,
    ) -> None:
        drafts = check_copypasta(
            db_pr, diff_with_duplication, copypasta_config, repo_with_duplicate
        )
        assert len(drafts) >= 1
        assert all(isinstance(d, ReviewDraft) for d in drafts)
        assert any("existing.py" in d.comment_body for d in drafts)

    @pytest.mark.integration
    @_skip_no_git
    def test_no_match_for_unique_code(
        self,
        db_pr: PullRequest,
        copypasta_config: ProjectConfig,
        repo_with_duplicate: Path,
    ) -> None:
        unique_patch = (
            "@@ -0,0 +1,5 @@\n"
            "+class UniqueSnowflake:\n"
            "+    def __init__(self, magic_number):\n"
            "+        self.magic = magic_number * 42\n"
            "+        self.sparkle = self.magic ** 2\n"
            "+        self.rainbow = hash(self.sparkle)\n"
        )
        diff = PRDiff(
            pr_number=42,
            files=(
                PRFileChange(
                    filename="unique.py",
                    status="added",
                    additions=5,
                    patch=unique_patch,
                ),
            ),
        )
        drafts = check_copypasta(db_pr, diff, copypasta_config, repo_with_duplicate)
        assert len(drafts) == 0

    @pytest.mark.integration
    @_skip_no_git
    def test_excludes_self_matches(
        self,
        db_pr: PullRequest,
        copypasta_config: ProjectConfig,
        repo_with_duplicate: Path,
    ) -> None:
        """Modifying a file should not match against itself in the repo."""
        # The repo has src/existing.py with calculate_total. If the PR
        # modifies that same file, it should NOT flag as copy-paste.
        patch = (
            "@@ -3,0 +3,5 @@\n"
            "+def calculate_total(items):\n"
            "+    total = 0\n"
            "+    for item in items:\n"
            "+        total += item.price * item.quantity\n"
            "+    return total\n"
        )
        diff = PRDiff(
            pr_number=42,
            files=(
                PRFileChange(
                    filename="src/existing.py",
                    status="modified",
                    additions=5,
                    patch=patch,
                ),
            ),
        )
        drafts = check_copypasta(db_pr, diff, copypasta_config, repo_with_duplicate)
        assert len(drafts) == 0

    def test_no_repo_files_returns_empty(
        self,
        db_pr: PullRequest,
        diff_with_duplication: PRDiff,
        copypasta_config: ProjectConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(copypasta_module, "_read_repo_files", lambda *a, **kw: {})
        result = check_copypasta(db_pr, diff_with_duplication, copypasta_config, tmp_path)
        assert result == []

    def test_winnowing_skips_already_matched_chunks(
        self,
        db_pr: PullRequest,
        diff_with_duplication: PRDiff,
        copypasta_config: ProjectConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_files = {"existing.py": "def calculate_total(items): pass\n"}
        monkeypatch.setattr(copypasta_module, "_read_repo_files", lambda *a, **kw: fake_files)

        # Symilar returns a match that covers the chunk from diff_with_duplication
        symilar_match = CopyPastaMatch(
            source_file="existing.py",
            source_start_line=1,
            source_end_line=6,
            new_file="src/utils.py",
            new_start_line=1,
            num_lines=6,
            tier="symilar",
        )
        monkeypatch.setattr(copypasta_module, "_check_symilar", lambda *a, **kw: [symilar_match])

        winnowing_called = False

        def spy_winnowing(chunks: list[Any], repo_files: dict[str, str]) -> list[CopyPastaMatch]:
            nonlocal winnowing_called
            winnowing_called = True
            return []

        monkeypatch.setattr(copypasta_module, "_check_winnowing", spy_winnowing)

        check_copypasta(db_pr, diff_with_duplication, copypasta_config, tmp_path)

        # Winnowing should be skipped entirely because symilar matched all chunks
        assert not winnowing_called

    def test_llm_tier_invoked_when_enabled(
        self,
        db_pr: PullRequest,
        diff_with_duplication: PRDiff,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        llm_config = ProjectConfig(
            owner="apache",
            repo="spark",
            copypasta_enabled=True,
            copypasta_min_lines=4,
            copypasta_scan_extensions=[".py"],
            copypasta_llm_enabled=True,
        )
        fake_files = {"existing.py": "def foo(): pass\n"}
        monkeypatch.setattr(copypasta_module, "_read_repo_files", lambda *a, **kw: fake_files)
        monkeypatch.setattr(copypasta_module, "_check_symilar", lambda *a, **kw: [])
        monkeypatch.setattr(copypasta_module, "_check_winnowing", lambda *a, **kw: [])

        llm_match = CopyPastaMatch(
            source_file="existing.py",
            source_start_line=1,
            source_end_line=1,
            new_file="src/utils.py",
            new_start_line=1,
            num_lines=6,
            tier="llm",
        )

        llm_called_with: list[Any] = []

        def fake_llm(chunks: list[Any], repo_files: dict[str, str]) -> list[CopyPastaMatch]:
            llm_called_with.extend(chunks)
            return [llm_match]

        monkeypatch.setattr(copypasta_module, "_check_llm", fake_llm)

        drafts = check_copypasta(db_pr, diff_with_duplication, llm_config, tmp_path)
        assert llm_called_with  # LLM was called with the unmatched chunks
        assert len(drafts) == 1
        assert "existing.py" in drafts[0].comment_body
