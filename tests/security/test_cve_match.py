"""Tests for the ASF public-CVE matcher (security.cve_match).

The matcher scores a report's title + parsed component against the titles
of the public CVEs the ASF has published for the report's project's PMC, and
stages ``valid`` on a strong match. No live service is hit — ``list_public_cves``
is patched so the tests don't depend on cveprocess.apache.org being up.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import (
    LLMBackendConfig,
    OperatorConfig,
    ProjectConfig,
    SecurityTriageConfig,
)
from franktheunicorn.security.cve_api import PublicCVE
from franktheunicorn.security.cve_match import (
    ALLOCATED_STATES,
    ASF_MATCH_THRESHOLD,
    ASFCVEMatch,
    allocated_cve_to_match_entry,
    extract_candidate_cve_ids,
    match_report_to_asf_cve,
    public_cve_to_match_entry,
)

pytestmark = pytest.mark.django_db


def _config() -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        llm_backends=[LLMBackendConfig(provider="stub")],
        security_triage=SecurityTriageConfig(enabled=True),
    )


class TestSimilarity:
    def test_obvious_match_scores_above_threshold(self) -> None:
        from franktheunicorn.security.cve_match import _similarity

        score = _similarity(
            "Apache Spark shell command injection via Spark UI",
            "Apache Spark shell command injection vulnerability via Spark UI",
        )
        assert score >= ASF_MATCH_THRESHOLD

    def test_unrelated_scores_low(self) -> None:
        from franktheunicorn.security.cve_match import _similarity

        assert _similarity("Spark shuffle OOM", "Apache Spark XSS in log viewer UI") < 0.3


class TestMatchReportToASFCVE:
    def _public(self, title: str, cve_id: str = "CVE-2022-33891") -> PublicCVE:
        return PublicCVE(cve_id=cve_id, title=title, state="PUBLIC", owner="spark")

    def test_match_above_threshold(self, db: Any) -> None:
        from tests.factories import ProjectFactory, SecurityReportFactory

        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(
            project=project,
            title="Spark shell command injection via UI",
            parsed_component="",
            raw_text="r",
            status="new",
        )
        project_config = ProjectConfig(owner="apache", repo="spark")
        with patch(
            "franktheunicorn.security.cve_match.list_public_cves",
            return_value=[
                self._public("Apache Spark shell command injection vulnerability via Spark UI")
            ],
        ):
            match = match_report_to_asf_cve(report, _config(), project_config=project_config)
        assert match is not None
        assert match.cve_id == "CVE-2022-33891"
        assert match.score >= ASF_MATCH_THRESHOLD

    def test_no_pmc_returns_none(self, db: Any) -> None:
        from tests.factories import ProjectFactory, SecurityReportFactory

        project = ProjectFactory(owner="holdenk", repo="my-app")
        report = SecurityReportFactory(project=project, title="whatever", raw_text="r")
        with patch("franktheunicorn.security.cve_match.list_public_cves") as mock_list:
            assert match_report_to_asf_cve(report, _config()) is None
            mock_list.assert_not_called()

    def test_no_project_returns_none(self, db: Any) -> None:
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(project=None, title="whatever", raw_text="r")
        assert match_report_to_asf_cve(report, _config()) is None

    def test_below_threshold_returns_none(self, db: Any) -> None:
        from tests.factories import ProjectFactory, SecurityReportFactory

        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(project=project, title="Spark shuffle OOM", raw_text="r")
        project_config = ProjectConfig(owner="apache", repo="spark")
        with patch(
            "franktheunicorn.security.cve_match.list_public_cves",
            return_value=[self._public("Apache Spark XSS in log viewer UI")],
        ):
            assert match_report_to_asf_cve(report, _config(), project_config=project_config) is None

    def test_empty_public_list_returns_none(self, db: Any) -> None:
        from tests.factories import ProjectFactory, SecurityReportFactory

        project = ProjectFactory(owner="apache", repo="spark")
        report = SecurityReportFactory(project=project, title="Spark RCE", raw_text="r")
        with patch("franktheunicorn.security.cve_match.list_public_cves", return_value=[]):
            assert match_report_to_asf_cve(report, _config()) is None


class TestPublicCVEToMatchEntry:
    def test_marks_asf_public_source_and_score(self) -> None:
        entry = public_cve_to_match_entry(
            ASFCVEMatch(
                cve_id="CVE-2021-38296",
                kind="public",
                title="Key Negotiation",
                score=0.82,
                state="PUBLIC",
            )
        )
        assert entry["source"] == "asf-public"
        assert entry["cve_id"] == "CVE-2021-38296"
        assert entry["match_score"] == 0.82
        assert entry["status"] == "PUBLIC"


class TestAllocatedCVEToMatchEntry:
    def test_marks_asf_allocated_source_and_state(self) -> None:
        entry = allocated_cve_to_match_entry("CVE-2026-5001", "REVIEW", "Spark shell injection")
        assert entry["source"] == "asf-allocated"
        assert entry["cve_id"] == "CVE-2026-5001"
        assert entry["status"] == "REVIEW"
        assert entry["description"] == "Spark shell injection"
        # An allocated match has no similarity score — it's an exact id.
        assert "match_score" not in entry


class TestExtractCandidateCVEIds:
    def _report(self, db: Any, **kwargs: Any):
        from tests.factories import SecurityReportFactory

        defaults = {"raw_text": "r", "title": "t", "status": "new"}
        defaults.update(kwargs)
        return SecurityReportFactory(**defaults)

    def test_pulls_id_from_matched_cve_id(self, db: Any) -> None:
        report = self._report(db, matched_cve_id="CVE-2026-5001")
        assert extract_candidate_cve_ids(report) == ["CVE-2026-5001"]

    def test_pulls_id_from_title(self, db: Any) -> None:
        report = self._report(db, title="Spark shell injection (CVE-2026-5001)")
        assert extract_candidate_cve_ids(report) == ["CVE-2026-5001"]

    def test_pulls_id_from_raw_text(self, db: Any) -> None:
        report = self._report(db, raw_text="See CVE-2026-5001 for context.")
        assert extract_candidate_cve_ids(report) == ["CVE-2026-5001"]

    def test_dedups_and_uppercases(self, db: Any) -> None:
        report = self._report(
            db,
            matched_cve_id="cve-2026-5001",
            title="Also cve-2026-5001 mentioned",
            raw_text="CVE-2026-5001 again",
        )
        assert extract_candidate_cve_ids(report) == ["CVE-2026-5001"]

    def test_no_ids_returns_empty(self, db: Any) -> None:
        report = self._report(db, raw_text="no cve here", title="nothing")
        assert extract_candidate_cve_ids(report) == []

    def test_multiple_ids_preserved_in_order(self, db: Any) -> None:
        report = self._report(
            db,
            raw_text="CVE-2026-5001 and CVE-2025-40404 both apply",
            title="",
            matched_cve_id="",
        )
        assert extract_candidate_cve_ids(report) == ["CVE-2026-5001", "CVE-2025-40404"]


class TestAllocatedStates:
    def test_public_is_not_allocated(self) -> None:
        assert "PUBLIC" not in ALLOCATED_STATES

    def test_reserved_through_ready_are_allocated(self) -> None:
        assert frozenset({"RESERVED", "DRAFT", "REVIEW", "READY"}) == ALLOCATED_STATES

    def test_reject_is_not_allocated(self) -> None:
        assert "REJECT" not in ALLOCATED_STATES
