"""Tests for the feedback formatter (v1.25)."""

from __future__ import annotations

import pytest

from franktheunicorn.review.feedback_formatter import format_feedback_markdown
from tests.factories import PullRequestFactory, ReviewDraftFactory, TestRunFactory


@pytest.mark.django_db
class TestFormatFeedbackMarkdown:
    def test_basic_good_assessment(self) -> None:
        pr = PullRequestFactory(
            number=42,
            title="Fix scheduler bug",
            author="alice",
        )
        result = format_feedback_markdown(pr, [], [], "good")
        assert "# Review Feedback: Good" in result
        assert "#42" in result
        assert "Fix scheduler bug" in result
        assert "alice" in result

    def test_needs_work_assessment(self) -> None:
        pr = PullRequestFactory(number=1, title="Test")
        result = format_feedback_markdown(pr, [], [], "needs-work")
        assert "Needs Work" in result

    def test_reject_assessment(self) -> None:
        pr = PullRequestFactory(number=1, title="Test")
        result = format_feedback_markdown(pr, [], [], "reject")
        assert "Reject" in result

    def test_findings_grouped_by_file(self) -> None:
        pr = PullRequestFactory(number=42, title="Test")
        d1 = ReviewDraftFactory(
            pull_request=pr,
            file_path="src/main.py",
            line_number=10,
            severity="important",
            category="correctness",
            comment_body="Off-by-one error here.",
        )
        d2 = ReviewDraftFactory(
            pull_request=pr,
            file_path="src/main.py",
            line_number=25,
            severity="nit",
            category="style",
            comment_body="Consider renaming this variable.",
        )
        d3 = ReviewDraftFactory(
            pull_request=pr,
            file_path="tests/test_main.py",
            line_number=5,
            severity="important",
            category="test-coverage",
            comment_body="Missing edge case test.",
        )
        result = format_feedback_markdown(pr, [d1, d2, d3], [], "needs-work")
        assert "### `src/main.py`" in result
        assert "### `tests/test_main.py`" in result
        assert "Off-by-one error" in result
        assert "Missing edge case" in result

    def test_finding_with_line_range(self) -> None:
        pr = PullRequestFactory(number=1, title="Test")
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="src/foo.py",
            line_number=10,
            line_end=15,
            comment_body="Refactor this block.",
        )
        result = format_feedback_markdown(pr, [draft], [], "needs-work")
        assert "L10-15" in result

    def test_finding_with_suggestion(self) -> None:
        pr = PullRequestFactory(number=1, title="Test")
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="src/foo.py",
            line_number=10,
            comment_body="Use a list comprehension.",
            suggestion="result = [x for x in items]",
        )
        result = format_feedback_markdown(pr, [draft], [], "needs-work")
        assert "```suggestion" in result
        assert "result = [x for x in items]" in result

    def test_no_findings(self) -> None:
        pr = PullRequestFactory(number=1, title="Test")
        result = format_feedback_markdown(pr, [], [], "good")
        assert "No specific findings." in result

    def test_test_runs_included(self) -> None:
        pr = PullRequestFactory(number=1, title="Test")
        run = TestRunFactory(
            pull_request=pr,
            run_type="pr_branch",
            status="completed",
            differential_verdict="suspect",
        )
        result = format_feedback_markdown(pr, [], [run], "needs-work")
        assert "## Test Verification" in result
        assert "pr_branch" in result
        assert "suspect" in result

    def test_test_run_with_error_log(self) -> None:
        pr = PullRequestFactory(number=1, title="Test")
        run = TestRunFactory(
            pull_request=pr,
            run_type="pr_branch",
            status="failed",
            differential_verdict="broken",
            error_log="AssertionError: expected 42 got 41",
        )
        result = format_feedback_markdown(pr, [], [run], "reject")
        assert "AssertionError" in result

    def test_general_finding_no_file(self) -> None:
        pr = PullRequestFactory(number=1, title="Test")
        draft = ReviewDraftFactory(
            pull_request=pr,
            file_path="",
            line_number=None,
            comment_body="Overall architecture concern.",
        )
        result = format_feedback_markdown(pr, [draft], [], "needs-work")
        assert "(general)" in result
        assert "Overall architecture concern." in result
