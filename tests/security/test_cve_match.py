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
    ASF_MATCH_THRESHOLD,
    ASFPublicCVEMatch,
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
            ASFPublicCVEMatch(
                cve_id="CVE-2021-38296", title="Key Negotiation", score=0.82, state="PUBLIC"
            )
        )
        assert entry["source"] == "asf-public"
        assert entry["cve_id"] == "CVE-2021-38296"
        assert entry["match_score"] == 0.82
        assert entry["status"] == "PUBLIC"
