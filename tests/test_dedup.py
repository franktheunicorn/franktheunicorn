"""Tests for finding deduplication across backends (§3.3)."""

from __future__ import annotations

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.dedup import deduplicate_findings


class TestDeduplicateFindings:
    def test_empty_list(self) -> None:
        assert deduplicate_findings([]) == []

    def test_single_finding(self) -> None:
        f = ReviewFinding(body="x", file_path="a.py", line_number=10)
        result = deduplicate_findings([f])
        assert len(result) == 1
        assert result[0].body == "x"

    def test_different_files_not_merged(self) -> None:
        findings = [
            ReviewFinding(body="x", file_path="a.py", line_number=10),
            ReviewFinding(body="y", file_path="b.py", line_number=10),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    def test_same_line_merged_keeps_longest_body(self) -> None:
        findings = [
            ReviewFinding(body="short", file_path="a.py", line_number=10),
            ReviewFinding(body="this is a much longer body", file_path="a.py", line_number=10),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0].body == "this is a much longer body"

    def test_nearby_lines_merged(self) -> None:
        findings = [
            ReviewFinding(body="first", file_path="a.py", line_number=10),
            ReviewFinding(body="second longer finding", file_path="a.py", line_number=12),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0].body == "second longer finding"

    def test_distant_lines_not_merged(self) -> None:
        findings = [
            ReviewFinding(body="first", file_path="a.py", line_number=10),
            ReviewFinding(body="second", file_path="a.py", line_number=100),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    def test_highest_confidence_used(self) -> None:
        findings = [
            ReviewFinding(body="a", file_path="a.py", line_number=10, confidence=0.3),
            ReviewFinding(body="ab", file_path="a.py", line_number=10, confidence=0.9),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0].confidence == 0.9

    def test_highest_severity_used(self) -> None:
        findings = [
            ReviewFinding(body="short", file_path="a.py", line_number=10, severity="nit"),
            ReviewFinding(
                body="longer finding body text",
                file_path="a.py",
                line_number=10,
                severity="critical",
            ),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0].severity == "critical"

    def test_suggestion_preserved_from_any_finding(self) -> None:
        findings = [
            ReviewFinding(
                body="has suggestion body text",
                file_path="a.py",
                line_number=10,
                suggestion="fix this",
            ),
            ReviewFinding(body="no suggestion but longer body text here", file_path="a.py", line_number=10),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0].suggestion == "fix this"

    def test_null_line_numbers_grouped(self) -> None:
        findings = [
            ReviewFinding(body="a", file_path="a.py"),
            ReviewFinding(body="ab", file_path="a.py"),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
