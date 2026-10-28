"""Tests for severity calibration (Mantis's /mantis-calibrate analog)."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import CalibrationConfig, ProjectConfig
from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.calibration import _score, calibrate_findings


def _finding(
    *,
    severity: str = "critical",
    confidence: float = 0.9,
    title: str = "security: shell injection from UI input",
    body: str = "The boundary is the Web UI; an attacker reaches this via "
    "the /api/submit endpoint with untrusted input that flows to Runtime.exec.",
) -> ReviewFinding:
    return ReviewFinding(
        file_path="src/main.py",
        line_number=42,
        title=title,
        body=body,
        confidence=confidence,
        severity=severity,
    )


def _cfg(**overrides: object) -> CalibrationConfig:
    base: dict[str, object] = {}
    base.update(overrides)
    return CalibrationConfig(**base)  # type: ignore[arg-type]


class TestScore:
    def test_critical_with_evidence_scores_high(self) -> None:
        assert _score(_finding()) >= 8.0

    def test_informational_scores_low(self) -> None:
        assert _score(_finding(severity="informational")) <= 3.0

    def test_low_confidence_drops_score(self) -> None:
        high = _score(_finding(confidence=1.0))
        low = _score(_finding(confidence=0.1))
        assert low < high

    def test_vague_title_penalized(self) -> None:
        concrete = _score(_finding(title="security: shell injection from UI"))
        vague = _score(_finding(title="security: potential issue to review"))
        assert vague < concrete

    def test_short_body_penalized(self) -> None:
        full = _score(_finding(body="A long body explaining the boundary and reachability."))
        thin = _score(_finding(body="looks off"))
        assert thin < full

    def test_score_clamped_1_to_10(self) -> None:
        assert _score(_finding(severity="critical", confidence=1.0)) <= 10.0
        assert _score(_finding(severity="informational", confidence=0.0)) >= 1.0

    def test_none_severity_uses_default(self) -> None:
        assert _score(_finding(severity="")) >= 4.0

    def test_unknown_severity_uses_default(self) -> None:
        assert _score(_finding(severity="bogus")) == _score(_finding(severity="unknown"))


class TestCalibrateFindings:
    def test_disabled_passes_through(self) -> None:
        findings = [_finding(), _finding(), _finding()]
        assert calibrate_findings(findings, _cfg(enabled=False)) == findings

    def test_caps_to_max_findings(self) -> None:
        findings = [_finding() for _ in range(10)]
        out = calibrate_findings(findings, _cfg(max_findings=3))
        assert len(out) == 3

    def test_drops_below_min_score(self) -> None:
        low = _finding(severity="informational", confidence=0.1, title="nit")
        high = _finding()
        out = calibrate_findings([low, high], _cfg(min_score=5.0))
        assert out == [high]

    def test_highest_score_surfaced_first(self) -> None:
        findings = [
            _finding(title="a"),
            _finding(title="b", confidence=0.2),
            _finding(title="c", confidence=1.0),
        ]
        out = calibrate_findings(findings, _cfg(max_findings=2))
        assert out[0].title == "c"

    def test_max_findings_zero_is_no_cap(self) -> None:
        findings = [_finding() for _ in range(20)]
        out = calibrate_findings(findings, _cfg(max_findings=0))
        assert len(out) == 20

    def test_cap_larger_than_count_is_noop(self) -> None:
        findings = [_finding(), _finding()]
        out = calibrate_findings(findings, _cfg(max_findings=50))
        assert len(out) == 2

    def test_empty_input(self) -> None:
        assert calibrate_findings([], _cfg()) == []


class TestWiring:
    def test_config_defaults_on(self) -> None:
        pc = ProjectConfig(owner="apache", repo="spark")
        assert pc.calibration.enabled is True
        assert pc.calibration.min_score == 3.0
        assert pc.calibration.max_findings == 5

    def test_config_validates_min_score(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _cfg(min_score=0.5)

    def test_config_validates_max_findings(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _cfg(max_findings=-1)
