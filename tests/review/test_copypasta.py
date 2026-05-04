"""Tests for the copy-pasta detection system."""

from __future__ import annotations

from pathlib import Path

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import PullRequest, ReviewDraft
from franktheunicorn.data_access.github.types import PRDiff, PRFileChange
from franktheunicorn.review.copypasta import (
    CopyPastaMatch,
    _check_llm,
    _check_symilar,
    _check_winnowing,
    _create_drafts,
    check_copypasta,
    extract_added_chunks,
)

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
    repo = tmp_path / "repo"
    repo.mkdir()

    # Initialize a git repo so git ls-files works
    import subprocess

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
        },
    )
    return repo


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    """Create an empty git repo."""
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    import subprocess

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


# -- Test tier 2: LLM stub --------------------------------------------------


class TestCheckLlm:
    def test_stub_returns_empty(self) -> None:
        from franktheunicorn.review.copypasta import CodeChunk

        chunks = [
            CodeChunk(file_path="new.py", start_line=1, lines=("line1", "line2", "line3", "line4"))
        ]
        matches = _check_llm(chunks, {"file.py": "content"})
        assert matches == []


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


# -- Test _read_repo_files ---------------------------------------------------


class TestReadRepoFiles:
    """Tests for the _read_repo_files helper."""

    def test_returns_files_filtered_by_extension(self, tmp_path) -> None:
        import subprocess

        from franktheunicorn.review.copypasta import _read_repo_files

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        (repo / "main.py").write_text("print('hello')\n")
        (repo / "notes.txt").write_text("some notes\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=repo,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "HOME": str(tmp_path),
            },
        )

        files = _read_repo_files(repo, extensions=[".py"], ignore_paths=[], exclude_files=None)
        assert "main.py" in files
        assert "notes.txt" not in files

    def test_respects_ignore_paths(self, tmp_path) -> None:
        import subprocess

        from franktheunicorn.review.copypasta import _read_repo_files

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "vendor").mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        (repo / "main.py").write_text("x = 1\n")
        (repo / "vendor" / "lib.py").write_text("y = 2\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=repo,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "HOME": str(tmp_path),
            },
        )

        files = _read_repo_files(
            repo, extensions=[".py"], ignore_paths=["vendor/"], exclude_files=None
        )
        assert "main.py" in files
        assert "vendor/lib.py" not in files

    def test_excludes_pr_changed_files(self, tmp_path) -> None:
        import subprocess

        from franktheunicorn.review.copypasta import _read_repo_files

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        (repo / "a.py").write_text("a = 1\n")
        (repo / "b.py").write_text("b = 2\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=repo,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "HOME": str(tmp_path),
            },
        )

        files = _read_repo_files(repo, extensions=[".py"], ignore_paths=[], exclude_files={"a.py"})
        assert "a.py" not in files
        assert "b.py" in files

    def test_git_failure_returns_empty(self, tmp_path) -> None:
        """If git ls-files fails (e.g. not a repo), return empty dict."""
        from franktheunicorn.review.copypasta import _read_repo_files

        # Non-git directory
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()

        files = _read_repo_files(not_a_repo, extensions=[".py"], ignore_paths=[])
        assert files == {}

    def test_oserror_reading_file_skips_gracefully(self, tmp_path) -> None:
        """OSError when reading a file should be skipped without crashing."""
        import subprocess
        from unittest.mock import patch

        from franktheunicorn.review.copypasta import _read_repo_files

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        (repo / "main.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=repo,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "HOME": str(tmp_path),
            },
        )

        with patch("pathlib.Path.read_text", side_effect=OSError("Permission denied")):
            files = _read_repo_files(repo, extensions=[".py"], ignore_paths=[])

        assert files == {}


# -- Test LLM tier in check_copypasta ----------------------------------------


@pytest.mark.django_db
class TestCheckCopypastaLLMTier:
    def test_llm_tier_enabled_calls_check_llm(
        self,
        db_pr,
        repo_with_duplicate,
    ) -> None:
        """When copypasta_llm_enabled=True and chunks are not matched by symilar/winnowing,
        _check_llm is called."""
        from unittest.mock import patch

        from franktheunicorn.config.models import ProjectConfig
        from franktheunicorn.data_access.github.types import PRDiff, PRFileChange
        from franktheunicorn.review.copypasta import check_copypasta

        # Use unique code that doesn't exist in repo_with_duplicate
        unique_patch = (
            "@@ -0,0 +1,5 @@\n"
            "+def totally_unique_function_xyz():\n"
            "+    alpha = 42 * 7\n"
            "+    beta = alpha + 13\n"
            "+    gamma = beta / 3\n"
            "+    return gamma\n"
        )
        diff = PRDiff(
            pr_number=42,
            raw_diff="",
            files=(
                PRFileChange(
                    filename="new_module.py",
                    status="added",
                    additions=5,
                    deletions=0,
                    patch=unique_patch,
                ),
            ),
        )

        config = ProjectConfig(
            owner="apache",
            repo="spark",
            copypasta_enabled=True,
            copypasta_min_lines=4,
            copypasta_scan_extensions=[".py"],
            copypasta_llm_enabled=True,
        )

        with patch(
            "franktheunicorn.review.copypasta._check_llm",
            return_value=[],
        ) as mock_llm:
            check_copypasta(db_pr, diff, config, repo_with_duplicate)

        # Unique code is not matched by symilar/winnowing → _check_llm must be called
        mock_llm.assert_called_once()


# -- Test extract_added_chunks: bad patch -----------------------------------


class TestExtractAddedChunksBadPatch:
    def test_unparseable_patch_is_skipped(self) -> None:
        """An unparseable patch should be skipped without raising."""
        from franktheunicorn.data_access.github.types import PRDiff, PRFileChange
        from franktheunicorn.review.copypasta import extract_added_chunks

        # A header-only hunk with no body causes unidiff to raise "Hunk is shorter than expected"
        diff = PRDiff(
            pr_number=1,
            files=(
                PRFileChange(
                    filename="broken.py",
                    status="modified",
                    patch="@@ -1,2 +1,3 @@ HEADER_BUT_NO_CONTENT",
                ),
            ),
        )
        chunks = extract_added_chunks(diff, min_lines=1)
        assert chunks == []


# -- Test _create_drafts: single-line location --------------------------------


@pytest.mark.django_db
class TestCreateDraftsSingleLineLocation:
    def test_single_line_source_omits_range(self, db_pr) -> None:
        """When source_start_line == source_end_line, comment omits line range."""
        from franktheunicorn.review.copypasta import CopyPastaMatch, _create_drafts

        matches = [
            CopyPastaMatch(
                source_file="existing.py",
                source_start_line=5,
                source_end_line=5,  # same → single-line location
                new_file="new.py",
                new_start_line=1,
                num_lines=4,
                tier="symilar",
            ),
        ]
        drafts = _create_drafts(db_pr, matches)
        assert len(drafts) == 1
        # Should not contain "lines X-Y" range, just the filename without a range
        assert "existing.py" in drafts[0].comment_body
        assert "lines 5" not in drafts[0].comment_body


# -- Test _check_winnowing edge cases ----------------------------------------


class TestCheckWinnowingEdgeCases:
    def test_empty_content_repo_file_is_skipped(self) -> None:
        """Repo files with only whitespace should be skipped gracefully."""
        from franktheunicorn.review.copypasta import CodeChunk, _check_winnowing

        chunks = [
            CodeChunk(
                file_path="new.py",
                start_line=1,
                lines=(
                    "def foo():",
                    "    x = 1",
                    "    y = 2",
                    "    return x + y",
                ),
            )
        ]
        repo_files = {"empty.py": "   \n\n  \n"}  # all whitespace
        # Should not crash
        matches = _check_winnowing(chunks, repo_files)
        assert isinstance(matches, list)

    def test_compare_files_exception_is_swallowed(self) -> None:
        """An exception from copydetect.compare_files should be swallowed."""
        from unittest.mock import patch

        from franktheunicorn.review.copypasta import CodeChunk, _check_winnowing

        code_lines = (
            "def calculate_total(items):",
            "    total = 0",
            "    for item in items:",
            "        total += item.price * item.quantity",
            "    return total",
        )
        chunks = [CodeChunk(file_path="new.py", start_line=1, lines=code_lines)]
        repo_files = {"existing.py": "\n".join(code_lines) + "\n"}

        with patch(
            "franktheunicorn.review.copypasta.compare_files",
            side_effect=RuntimeError("comparison failed"),
        ):
            matches = _check_winnowing(chunks, repo_files)

        assert matches == []
