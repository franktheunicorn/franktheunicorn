"""
Finding deduplication across multiple LLM backends (§3.3).

When multiple backends flag the same file/line region, merges into a
single finding with the most detailed body.
"""

from __future__ import annotations

from franktheunicorn.review.backends.base import ReviewFinding

# Findings within this many lines of each other are considered duplicates.
_LINE_PROXIMITY = 3


def deduplicate_findings(
    findings: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Deduplicate findings that target the same file/line region.

    Groups by file_path where line numbers are within ``_LINE_PROXIMITY``
    of each other. Within each group, keeps the finding with the longest body.
    """
    if len(findings) <= 1:
        return findings

    # Group by file, then merge nearby lines within each file group.
    by_file: dict[str, list[ReviewFinding]] = {}
    for finding in findings:
        by_file.setdefault(finding.file_path, []).append(finding)

    groups: list[list[ReviewFinding]] = []
    for file_findings in by_file.values():
        sorted_f = sorted(file_findings, key=lambda f: f.line_number or 0)
        current_group: list[ReviewFinding] = [sorted_f[0]]
        for f in sorted_f[1:]:
            prev_line = current_group[-1].line_number or 0
            curr_line = f.line_number or 0
            if abs(curr_line - prev_line) <= _LINE_PROXIMITY:
                current_group.append(f)
            else:
                groups.append(current_group)
                current_group = [f]
        groups.append(current_group)

    result: list[ReviewFinding] = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
            continue

        # Pick the finding with the longest body as the primary.
        primary = max(group, key=lambda f: len(f.body))

        # Use the highest confidence from the group.
        best_confidence = max(f.confidence for f in group)

        # Use the highest severity from the group.
        severity_rank = {"critical": 4, "important": 3, "nit": 1, "informational": 0}
        best_severity = max(group, key=lambda f: severity_rank.get(f.severity, 2)).severity

        result.append(
            ReviewFinding(
                file_path=primary.file_path,
                line_number=primary.line_number,
                title=primary.title,
                body=primary.body,
                suggestion=primary.suggestion
                or next((f.suggestion for f in group if f.suggestion), ""),
                confidence=best_confidence,
                severity=best_severity,
            )
        )

    return result
