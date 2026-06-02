"""Tests for RLM result aggregation."""

from __future__ import annotations

from franktheunicorn.review.backends.base import ReviewFinding, ReviewResult
from franktheunicorn.review.rlm.aggregate import (
    aggregate_review,
    interest_label_from_findings,
    merge_vibes,
)


def test_merge_vibes_dedups_and_caps() -> None:
    assert merge_vibes(["a", "a", "b", "", "  "]) == "a b"
    assert len(merge_vibes([f"v{i}" for i in range(10)]).split()) == 5


def test_aggregate_dedups_same_location_findings() -> None:
    f1 = ReviewFinding(
        file_path="x.py", line_number=10, body="missing a null check here", severity="nit"
    )
    f2 = ReviewFinding(
        file_path="x.py", line_number=10, body="missing a null check here too", severity="important"
    )
    r1 = ReviewResult(overall_vibe="v1", findings=[f1])
    r2 = ReviewResult(overall_vibe="v2", findings=[f2])

    out = aggregate_review([r1, r2])
    assert len(out.findings) == 1
    # Highest severity wins on merge.
    assert out.findings[0].severity == "important"
    assert out.overall_vibe == "v1 v2"


def test_aggregate_keeps_distinct_files() -> None:
    f1 = ReviewFinding(file_path="a.py", line_number=1, body="issue a")
    f2 = ReviewFinding(file_path="b.py", line_number=1, body="issue b")
    out = aggregate_review([ReviewResult(findings=[f1, f2])])
    assert len(out.findings) == 2


def test_synthesized_vibe_overrides_merge() -> None:
    out = aggregate_review([ReviewResult(overall_vibe="leaf")], synthesized_vibe="synth")
    assert out.overall_vibe == "synth"


def test_interest_label_mapping() -> None:
    assert interest_label_from_findings([]) is None
    assert interest_label_from_findings([ReviewFinding(body="x", severity="critical")]) == "high"
    assert interest_label_from_findings([ReviewFinding(body="x", severity="nit")]) == "medium"
    many = [ReviewFinding(body=f"f{i}", severity="nit") for i in range(3)]
    assert interest_label_from_findings(many) == "high"
