"""Tests for finding deduplication across backends (§3.3)."""

from __future__ import annotations

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.dedup import (
    _jaccard_similarity,
    _should_merge,
    deduplicate_findings,
    merge_source_tags,
)


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

    def test_nearby_lines_merged_with_overlap(self) -> None:
        findings = [
            ReviewFinding(
                body="The null check here is incorrect",
                file_path="a.py",
                line_number=10,
            ),
            ReviewFinding(
                body="The null check here is incorrect and should use isNullAt instead",
                file_path="a.py",
                line_number=12,
            ),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert "isNullAt" in result[0].body

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
            ReviewFinding(
                body="no suggestion but longer body text here", file_path="a.py", line_number=10
            ),
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


class TestJaccardSimilarity:
    def test_identical(self) -> None:
        assert _jaccard_similarity("the quick brown fox", "the quick brown fox") == 1.0

    def test_no_overlap(self) -> None:
        assert _jaccard_similarity("hello world", "goodbye universe") == 0.0

    def test_partial_overlap(self) -> None:
        sim = _jaccard_similarity(
            "The null check is wrong here",
            "The null check should use isNullAt",
        )
        assert sim > 0.3  # "the", "null", "check" overlap

    def test_empty_text(self) -> None:
        assert _jaccard_similarity("", "hello") == 0.0
        assert _jaccard_similarity("", "") == 0.0


class TestShouldMerge:
    def test_same_line_always_merges(self) -> None:
        a = ReviewFinding(body="completely different", file_path="a.py", line_number=10)
        b = ReviewFinding(body="unrelated content", file_path="a.py", line_number=10)
        assert _should_merge(a, b) is True

    def test_different_files_never_merge(self) -> None:
        a = ReviewFinding(body="same content", file_path="a.py", line_number=10)
        b = ReviewFinding(body="same content", file_path="b.py", line_number=10)
        assert _should_merge(a, b) is False

    def test_nearby_with_overlap_merges(self) -> None:
        a = ReviewFinding(
            body="The null check here is wrong",
            file_path="a.py",
            line_number=10,
        )
        b = ReviewFinding(
            body="The null check here should use isNullAt",
            file_path="a.py",
            line_number=13,
        )
        assert _should_merge(a, b) is True

    def test_nearby_without_overlap_no_merge(self) -> None:
        a = ReviewFinding(body="fix import order", file_path="a.py", line_number=10)
        b = ReviewFinding(body="add return type annotation", file_path="a.py", line_number=14)
        assert _should_merge(a, b) is False

    def test_distant_lines_no_merge(self) -> None:
        a = ReviewFinding(body="same content", file_path="a.py", line_number=10)
        b = ReviewFinding(body="same content", file_path="a.py", line_number=100)
        assert _should_merge(a, b) is False


class TestCrossSourceDedup:
    def test_agent_and_coderabbit_merged(self) -> None:
        agent_finding = ReviewFinding(
            body="The null check on line 42 is incorrect, use isNullAt instead",
            file_path="core/RDD.scala",
            line_number=42,
            severity="important",
            confidence=0.8,
        )
        cr_finding = ReviewFinding(
            body="Null check incorrect: use isNullAt(idx) instead of == null",
            file_path="core/RDD.scala",
            line_number=42,
            severity="critical",
            confidence=0.7,
        )
        result = deduplicate_findings([agent_finding, cr_finding])
        assert len(result) == 1
        assert result[0].severity == "critical"  # highest severity
        assert result[0].confidence == 0.8  # highest confidence

    def test_merge_source_tags(self) -> None:
        findings = [
            ReviewFinding(body="null check wrong here", file_path="a.py", line_number=10),
            ReviewFinding(body="null check wrong use isNullAt", file_path="a.py", line_number=10),
        ]
        sources = ["agent", "coderabbit"]
        deduped = deduplicate_findings(findings)
        tags = merge_source_tags(findings, sources, deduped)
        assert len(tags) == 1
        # Both sources should be present (merged via fuzzy match on nearby lines).
        assert "agent" in tags[0] or "coderabbit" in tags[0]

    def test_no_merge_different_issues(self) -> None:
        findings = [
            ReviewFinding(
                body="Missing null check here",
                file_path="a.py",
                line_number=10,
            ),
            ReviewFinding(
                body="Style: use snake_case for variable names",
                file_path="a.py",
                line_number=50,
            ),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2
