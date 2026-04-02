"""Tests for CodeRabbit CLI integration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import CodeRabbitConfig
from franktheunicorn.core.models import Project, PullRequest
from franktheunicorn.review.coderabbit import (
    CodeRabbitFinding,
    create_drafts_from_coderabbit,
    parse_prompt_only_output,
    run_coderabbit_review,
)
from tests.factories import AntiPatternFactory

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------


class TestParsePromptOnlyOutput:
    def test_parse_fixture_file(self) -> None:
        raw = (FIXTURES_DIR / "coderabbit_output.txt").read_text()
        findings = parse_prompt_only_output(raw)
        assert len(findings) == 3

        # First finding — critical severity.
        assert findings[0].file_path == "src/main.py"
        assert findings[0].line_number == 42
        assert findings[0].severity == "critical"
        assert "Race condition" in findings[0].title
        assert findings[0].suggestion != ""

        # Second finding — high severity.
        assert findings[1].file_path == "src/utils/parser.py"
        assert findings[1].line_number == 118
        assert findings[1].severity == "high"

        # Third finding — nit.
        assert findings[2].severity == "nit"

    def test_empty_output(self) -> None:
        assert parse_prompt_only_output("") == []
        assert parse_prompt_only_output("   \n  ") == []

    def test_clean_run_no_findings(self) -> None:
        output = "Review completed ✔\nNo issues found."
        assert parse_prompt_only_output(output) == []

    def test_single_finding_no_separator(self) -> None:
        output = "src/foo.py:10 - [Medium] Unused import\n\nRemove the unused import.\n\n**Suggestion:** Delete line 10."
        findings = parse_prompt_only_output(output)
        assert len(findings) == 1
        assert findings[0].severity == "medium"
        assert findings[0].suggestion == "Delete line 10."

    def test_unparseable_block_captured_as_fallback(self) -> None:
        """Substantial blocks that don't match the header pattern are still captured."""
        output = (
            "This block has no standard header format but contains\n"
            "useful review feedback that should not be silently dropped\n"
            "because the parser couldn't match the regex."
        )
        findings = parse_prompt_only_output(output)
        assert len(findings) == 1
        assert findings[0].file_path == ""
        assert findings[0].line_number is None
        assert findings[0].severity == "medium"
        assert "useful review feedback" in findings[0].body


# ---------------------------------------------------------------------------
# CLI runner tests (mocked subprocess)
# ---------------------------------------------------------------------------


class TestRunCodeRabbitReview:
    def _config(self) -> CodeRabbitConfig:
        return CodeRabbitConfig(enabled=True, cli_path="coderabbit")

    @patch("franktheunicorn.review.coderabbit.subprocess.run")
    def test_success(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(FIXTURES_DIR / "coderabbit_output.txt").read_text(),
            stderr="",
        )
        findings = run_coderabbit_review("/tmp/repo", "abc123", self._config())
        assert len(findings) == 3
        mock_run.assert_called_once()

    @patch("franktheunicorn.review.coderabbit.subprocess.run")
    def test_cli_not_found(self, mock_run: Any) -> None:
        mock_run.side_effect = FileNotFoundError("coderabbit not found")
        findings = run_coderabbit_review("/tmp/repo", "abc123", self._config())
        assert findings == []

    @patch("franktheunicorn.review.coderabbit.subprocess.run")
    def test_cli_timeout(self, mock_run: Any) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="coderabbit", timeout=120)
        findings = run_coderabbit_review("/tmp/repo", "abc123", self._config())
        assert findings == []

    @patch("franktheunicorn.review.coderabbit.subprocess.run")
    def test_nonzero_exit(self, mock_run: Any) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error"
        )
        findings = run_coderabbit_review("/tmp/repo", "abc123", self._config())
        assert findings == []


# ---------------------------------------------------------------------------
# Draft creation tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateDraftsFromCodeRabbit:
    def test_creates_drafts_with_coderabbit_source(self, db_pr: PullRequest) -> None:
        findings = [
            CodeRabbitFinding(
                file_path="src/main.py",
                line_number=42,
                severity="high",
                title="Bug found",
                body="Bug found\n\nDetails here.",
                suggestion="Fix it.",
            ),
        ]
        drafts = create_drafts_from_coderabbit(db_pr, findings)
        assert len(drafts) == 1
        assert "coderabbit" in drafts[0].sources
        assert "Bug found" in drafts[0].comment_body
        assert (
            "[CodeRabbit]" not in drafts[0].comment_body
        )  # attribution via source field, not prefix
        assert drafts[0].suggestion == "Fix it."
        assert drafts[0].status == "pending"

    def test_confidence_mapping(self, db_pr: PullRequest) -> None:
        findings = [
            CodeRabbitFinding("a.py", 1, "critical", "t", "t"),
            CodeRabbitFinding("b.py", 2, "high", "t", "t"),
            CodeRabbitFinding("c.py", 3, "medium", "t", "t"),
            CodeRabbitFinding("d.py", 4, "nit", "t", "t"),
            CodeRabbitFinding("e.py", 5, "unknown", "t", "t"),
        ]
        drafts = create_drafts_from_coderabbit(db_pr, findings)
        confidences = [d.confidence for d in drafts]
        assert confidences == [0.9, 0.8, 0.6, 0.3, 0.5]

    def test_anti_pattern_suppression(self, db_pr: PullRequest, db_project: Project) -> None:
        AntiPatternFactory(
            pattern_text="nit:",
            project=db_project,
        )
        findings = [
            CodeRabbitFinding("a.py", 1, "nit", "nit: spacing", "nit: spacing issue"),
            CodeRabbitFinding("b.py", 2, "high", "Bug found", "Bug found"),
        ]
        drafts = create_drafts_from_coderabbit(db_pr, findings, project=db_project)
        # "nit: spacing" in the body matches anti-pattern "nit:", so it's suppressed.
        assert len(drafts) == 1
        assert drafts[0].file_path == "b.py"

    def test_anti_pattern_actually_suppresses(
        self, db_pr: PullRequest, db_project: Project
    ) -> None:
        AntiPatternFactory(
            pattern_text="spacing issue",
            project=db_project,
        )
        findings = [
            CodeRabbitFinding("a.py", 1, "nit", "Spacing", "Fix spacing issue here"),
            CodeRabbitFinding("b.py", 2, "high", "Bug", "Real bug found"),
        ]
        drafts = create_drafts_from_coderabbit(db_pr, findings, project=db_project)
        assert len(drafts) == 1
        assert drafts[0].file_path == "b.py"

    def test_empty_findings(self, db_pr: PullRequest) -> None:
        drafts = create_drafts_from_coderabbit(db_pr, [])
        assert drafts == []


# ---------------------------------------------------------------------------
# Worker integration test
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkerCodeRabbitIntegration:
    @patch("franktheunicorn.worker.runner._resolve_base_ref", return_value="origin/main")
    @patch("franktheunicorn.review.coderabbit.subprocess.run")
    def test_run_coderabbit_for_pr_creates_drafts(
        self,
        mock_subprocess: Any,
        mock_resolve: Any,
        db_pr: PullRequest,
        tmp_path: Path,
    ) -> None:
        from franktheunicorn.core.models import ReviewDraft
        from franktheunicorn.worker.runner import _run_coderabbit_for_pr

        # Set up the repo path so it exists.
        repo_path = tmp_path / ".review-agent" / "repos" / db_pr.project.full_name
        repo_path.mkdir(parents=True)

        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(FIXTURES_DIR / "coderabbit_output.txt").read_text(),
            stderr="",
        )

        config = CodeRabbitConfig(enabled=True, cli_path="coderabbit")

        with patch("franktheunicorn.worker.runner.Path.home", return_value=tmp_path):
            _run_coderabbit_for_pr(db_pr, config)

        cr_drafts = [
            d
            for d in ReviewDraft.objects.filter(pull_request=db_pr)
            if "coderabbit" in (d.sources or [])
        ]
        assert len(cr_drafts) == 3
