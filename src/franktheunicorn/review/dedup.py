"""
Finding deduplication across multiple LLM backends and CodeRabbit (§3.3).

When multiple backends or CodeRabbit flag the same file/line region, merges
into a single finding with the most detailed body and combined sources.

v1.5 enhancement: fuzzy matching using line proximity + keyword overlap
(Jaccard similarity > 0.3) for cross-source deduplication.
"""

from __future__ import annotations

import re

from franktheunicorn.review.backends.base import ReviewFinding

# Findings within this many lines of each other are considered duplicates.
_LINE_PROXIMITY = 5

# Minimum Jaccard similarity to consider two findings as duplicates.
_JACCARD_THRESHOLD = 0.3

# Pattern to tokenize text into words.
_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _tokenize(text: str) -> set[str]:
    """Extract normalized word tokens from text."""
    return {w.lower() for w in _WORD_RE.findall(text)}


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between tokenized texts.

    Returns a value between 0.0 (no overlap) and 1.0 (identical tokens).
    """
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _is_substring_match(text_a: str, text_b: str) -> bool:
    """Check if one text body is a substantial substring of the other."""
    a = text_a.strip().lower()
    b = text_b.strip().lower()
    if len(a) < 20 or len(b) < 20:
        return False
    return a in b or b in a


def _should_merge(a: ReviewFinding, b: ReviewFinding) -> bool:
    """Determine if two findings should be merged.

    Criteria:
    - Same file path AND
    - Same exact line (distance 0) → always merge, OR
    - Adjacent lines (within _LINE_PROXIMITY) AND
      (Jaccard > _JACCARD_THRESHOLD OR one is substring of the other)
    """
    if a.file_path != b.file_path:
        return False

    line_a = a.line_number or 0
    line_b = b.line_number or 0
    distance = abs(line_a - line_b)

    if distance > _LINE_PROXIMITY:
        return False

    # Same exact line: always merge (clearly about the same code location).
    if distance == 0:
        return True

    # Adjacent but different lines: require content similarity.
    jaccard = _jaccard_similarity(a.body, b.body)
    if jaccard >= _JACCARD_THRESHOLD:
        return True

    return _is_substring_match(a.body, b.body)


def is_duplicate_finding(
    file_a: str,
    line_a: int | None,
    body_a: str,
    file_b: str,
    line_b: int | None,
    body_b: str,
) -> bool:
    """Field-level duplicate check reusing the same heuristic as ``_should_merge``.

    Works on the raw ``(file, line, body)`` triples of a finding rather than a
    ``ReviewFinding`` object, so callers holding persisted ``ReviewDraft`` rows
    (e.g. cross-agent CLI dedup) can compare against fresh findings without
    constructing throwaway objects. Same file + exact line always matches; a
    nearby line matches only when the bodies are similar (Jaccard) or one is a
    substring of the other.
    """
    if file_a != file_b:
        return False
    distance = abs((line_a or 0) - (line_b or 0))
    if distance > _LINE_PROXIMITY:
        return False
    if distance == 0:
        return True
    if _jaccard_similarity(body_a, body_b) >= _JACCARD_THRESHOLD:
        return True
    return _is_substring_match(body_a, body_b)


def deduplicate_findings(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Deduplicate findings that target the same file/line region.

    Uses fuzzy matching: file + line proximity (within 5 lines) + keyword
    overlap (Jaccard > 0.3) or substring matching.

    Returns a list of deduplicated findings.
    """
    deduped, _ = deduplicate_findings_with_groups(findings)
    return deduped


def deduplicate_findings_with_groups(
    findings: list[ReviewFinding],
) -> tuple[list[ReviewFinding], list[list[int]]]:
    """Deduplicate findings and report which inputs formed each output.

    Returns ``(deduped, groups)`` where ``groups[i]`` lists the indices into
    ``findings`` that were merged into ``deduped[i]``. Callers use the groups
    to combine per-finding metadata (e.g. source attribution) exactly, rather
    than reconstructing membership by key matching after the fact.
    """
    if len(findings) <= 1:
        return findings, [[i] for i in range(len(findings))]

    # Group by file, then merge nearby+similar findings within each file.
    by_file: dict[str, list[tuple[int, ReviewFinding]]] = {}
    for idx, finding in enumerate(findings):
        by_file.setdefault(finding.file_path, []).append((idx, finding))

    groups: list[list[tuple[int, ReviewFinding]]] = []
    for file_findings in by_file.values():
        sorted_f = sorted(file_findings, key=lambda t: t[1].line_number or 0)

        # Build groups using fuzzy matching.
        used: set[int] = set()
        for i, (idx_a, fa) in enumerate(sorted_f):
            if idx_a in used:
                continue
            group: list[tuple[int, ReviewFinding]] = [(idx_a, fa)]
            used.add(idx_a)
            for j in range(i + 1, len(sorted_f)):
                idx_b, fb = sorted_f[j]
                if idx_b in used:
                    continue
                if _should_merge(fa, fb):
                    group.append((idx_b, fb))
                    used.add(idx_b)
            groups.append(group)

    result: list[ReviewFinding] = []
    result_groups: list[list[int]] = []
    for group in groups:
        findings_in_group = [f for _, f in group]
        result_groups.append([idx for idx, _ in group])

        if len(findings_in_group) == 1:
            result.append(findings_in_group[0])
            continue

        # Pick the finding with the longest body as the primary.
        primary = max(findings_in_group, key=lambda f: len(f.body))

        # Use the highest confidence from the group.
        best_confidence = max(f.confidence for f in findings_in_group)

        # Use the highest severity from the group.
        severity_rank = {"critical": 4, "important": 3, "nit": 1, "informational": 0}
        best_severity = max(
            findings_in_group, key=lambda f: severity_rank.get(f.severity, 2)
        ).severity

        # Collect suggestion from any finding in the group.
        suggestion = primary.suggestion or next(
            (f.suggestion for f in findings_in_group if f.suggestion), ""
        )

        # Build combined title with all reasoning traces.
        titles = [f.title for f in findings_in_group if f.title and f.title != primary.title]
        combined_title = primary.title
        if titles:
            combined_title += " | " + " | ".join(titles[:3])

        result.append(
            ReviewFinding(
                file_path=primary.file_path,
                line_number=primary.line_number,
                title=combined_title,
                body=primary.body,
                suggestion=suggestion,
                confidence=best_confidence,
                severity=best_severity,
            )
        )

    return result, result_groups


def merge_source_tags_from_groups(
    finding_sources: list[str],
    groups: list[list[int]],
) -> list[str]:
    """Combine per-finding source tags using dedup group membership.

    Returns a list of comma-separated source strings parallel to the deduped
    findings (e.g. ``"agent,coderabbit"`` when both backends contributed to a
    merged finding). ``groups`` comes from ``deduplicate_findings_with_groups``.
    """
    result: list[str] = []
    for group in groups:
        # Deduplicate sources while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for idx in group:
            source = finding_sources[idx] if idx < len(finding_sources) else "agent"
            if source and source not in seen:
                seen.add(source)
                unique.append(source)
        result.append(",".join(unique) if unique else "agent")
    return result
