"""Tests for finding deduplication across backends (§3.3)."""

from __future__ import annotations

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.dedup import (
    _jaccard_similarity,
    _should_merge,
    deduplicate_findings,
    deduplicate_findings_with_groups,
    merge_source_tags_from_groups,
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

    def test_grouping_is_greedy_not_transitive(self) -> None:
        """Pin down the documented non-transitive merge behavior.

        ``deduplicate_findings`` only tests the group anchor (the first
        finding) against every later finding. So for findings on lines
        10 / 14 / 18 (anchor distance 0/4/8 with proximity threshold 5):
        - 10 and 14 merge (within 5 lines, overlapping body)
        - 10 and 18 do NOT merge (distance 8 exceeds proximity)
        - 14 and 18 are never compared because 14 was already absorbed
          into the anchor's group
        Result: two groups, not one. This is intentional (avoids
        runaway transitive merges) but is worth a regression test so
        a future tweak doesn't silently change the contract.
        """
        a = ReviewFinding(file_path="a.py", line_number=10, body="The null check is wrong")
        b = ReviewFinding(file_path="a.py", line_number=14, body="The null check is wrong indeed")
        c = ReviewFinding(file_path="a.py", line_number=18, body="The null check is wrong here")

        result = deduplicate_findings([a, b, c])
        # Two groups: {a, b} merged into one, c on its own. The merged
        # group's primary is b (longest body), so the kept line numbers
        # are b's (14) and c's (18). What matters for the contract is
        # that there are two findings, not one transitive group of three.
        assert len(result) == 2
        line_numbers = sorted(f.line_number for f in result if f.line_number is not None)
        assert line_numbers == [14, 18]


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

    def test_merge_source_tags_same_line(self) -> None:
        findings = [
            ReviewFinding(body="null check wrong here", file_path="a.py", line_number=10),
            ReviewFinding(body="null check wrong use isNullAt", file_path="a.py", line_number=10),
        ]
        sources = ["agent", "coderabbit"]
        deduped, groups = deduplicate_findings_with_groups(findings)
        assert len(deduped) == 1
        tags = merge_source_tags_from_groups(sources, groups)
        assert len(tags) == 1
        # Both contributors must be attributed on the merged finding.
        assert tags[0] == "agent,coderabbit"

    def test_merge_source_tags_fuzzy_nearby_lines(self) -> None:
        # Different wording and nearby-but-different lines: the merged finding
        # keeps the longer body, but attribution must still include both
        # backends (regression: key-based reconstruction only found the
        # primary's source).
        findings = [
            ReviewFinding(
                body="missing null check on user input", file_path="a.py", line_number=10
            ),
            ReviewFinding(
                body="null check missing for the user input value here",
                file_path="a.py",
                line_number=12,
            ),
        ]
        sources = ["agent", "coderabbit"]
        deduped, groups = deduplicate_findings_with_groups(findings)
        assert len(deduped) == 1
        tags = merge_source_tags_from_groups(sources, groups)
        assert tags == ["agent,coderabbit"]

    def test_merge_source_tags_repeated_source_deduped(self) -> None:
        findings = [
            ReviewFinding(body="null check wrong here", file_path="a.py", line_number=10),
            ReviewFinding(body="null check wrong here too", file_path="a.py", line_number=10),
        ]
        _deduped, groups = deduplicate_findings_with_groups(findings)
        tags = merge_source_tags_from_groups(["agent", "agent"], groups)
        assert tags == ["agent"]

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
