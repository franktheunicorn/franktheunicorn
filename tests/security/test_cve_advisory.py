"""Tests for the CVE 5 advisory builder (security.cve_advisory)."""

from __future__ import annotations

import pytest

from franktheunicorn.security.cve_advisory import build_cve5_json
from tests.factories import SecurityReportFactory


@pytest.mark.django_db
class TestBuildFresh:
    def test_skeleton_mirrors_the_allocate_route(self) -> None:
        report = SecurityReportFactory(
            title="Overflow in the widget",
            matched_cve_id="CVE-2026-12345",
            parsed_impact="Remote code execution via the widget parser.",
        )
        record = build_cve5_json(report, pmc="spark")
        assert record["dataType"] == "CVE_RECORD"
        assert record["cveMetadata"]["cveId"] == "CVE-2026-12345"
        # What allocatecve.js creates: public PUBLISHED, private RESERVED.
        assert record["cveMetadata"]["state"] == "PUBLISHED"
        assert record["CNA_private"] == {"owner": "spark", "state": "RESERVED"}
        cna = record["containers"]["cna"]
        assert cna["title"] == "Overflow in the widget"
        assert cna["descriptions"] == [
            {"lang": "en", "value": "Remote code execution via the widget parser."}
        ]

    def test_description_preference_order(self) -> None:
        report = SecurityReportFactory(
            title="The title",
            matched_cve_id="CVE-2026-12345",
            parsed_impact="",
            triage_summary="The summary",
        )
        record = build_cve5_json(report, pmc="spark")
        assert record["containers"]["cna"]["descriptions"][0]["value"] == "The summary"

    def test_affected_carries_the_operators_sentence(self) -> None:
        report = SecurityReportFactory(
            matched_cve_id="CVE-2026-12345",
            affected_versions="3.5.0 to 3.5.2, 4.0.0 before 4.0.1",
        )
        report.project.repo = "spark"
        record = build_cve5_json(report, pmc="spark")
        assert record["containers"]["cna"]["affected"] == [
            {
                "vendor": "Apache",
                "product": "spark",
                "versions": [
                    {"version": "3.5.0 to 3.5.2, 4.0.0 before 4.0.1", "status": "affected"}
                ],
            }
        ]

    def test_no_cvss_is_fabricated(self) -> None:
        report = SecurityReportFactory(matched_cve_id="CVE-2026-12345", assessed_severity="high")
        record = build_cve5_json(report, pmc="spark")
        assert "metrics" not in record["containers"]["cna"]

    def test_no_cve_id_raises(self) -> None:
        report = SecurityReportFactory(matched_cve_id="")
        with pytest.raises(ValueError, match="matched_cve_id"):
            build_cve5_json(report, pmc="spark")


@pytest.mark.django_db
class TestReferences:
    def test_fix_branch_and_thread_links(self) -> None:
        report = SecurityReportFactory(
            matched_cve_id="CVE-2026-12345",
            fixed_in_branch="master",
            fix_branch="fix-bug-42",
            email_message_id="abc123",
        )
        report.project.owner = "apache"
        report.project.repo = "spark"
        refs = build_cve5_json(report, pmc="spark")["containers"]["cna"]["references"]
        urls = [r["url"] for r in refs]
        assert "https://github.com/apache/spark/tree/fix-bug-42" in urls
        assert "https://github.com/apache/spark/tree/master" in urls
        assert "https://lists.apache.org/thread/abc123" in urls

    def test_free_text_fixed_in_branch_is_not_urlified(self) -> None:
        report = SecurityReportFactory(
            matched_cve_id="CVE-2026-12345",
            fixed_in_branch="master, branch-4.0 (backport pending)",
        )
        refs = build_cve5_json(report, pmc="spark")["containers"]["cna"].get("references", [])
        assert all("backport" not in r["url"] for r in refs)

    def test_no_project_no_references(self) -> None:
        report = SecurityReportFactory(matched_cve_id="CVE-2026-12345", project=None)
        record = build_cve5_json(report, pmc="spark")
        assert "references" not in record["containers"]["cna"]


@pytest.mark.django_db
class TestMergeExisting:
    def _existing(self) -> dict:
        return {
            "dataType": "CVE_RECORD",
            "dataVersion": "5.0",
            "cveMetadata": {"cveId": "CVE-2026-12345", "serial": 3, "state": "PUBLISHED"},
            "CNA_private": {"owner": "spark", "userslist": ["a@apache.org"], "state": "RESERVED"},
            "containers": {
                "cna": {"title": "Old title", "metrics": [{"cvssV3_1": {"baseScore": 7.5}}]}
            },
        }

    def test_metadata_and_private_survive_verbatim(self) -> None:
        report = SecurityReportFactory(
            title="New title",
            matched_cve_id="CVE-2026-12345",
            parsed_impact="New impact.",
        )
        record = build_cve5_json(report, pmc="spark", existing=self._existing())
        assert record["cveMetadata"] == {
            "cveId": "CVE-2026-12345",
            "serial": 3,
            "state": "PUBLISHED",
        }
        assert record["CNA_private"]["userslist"] == ["a@apache.org"]

    def test_managed_cna_fields_are_replaced_others_kept(self) -> None:
        report = SecurityReportFactory(
            title="New title",
            matched_cve_id="CVE-2026-12345",
            parsed_impact="New impact.",
        )
        record = build_cve5_json(report, pmc="spark", existing=self._existing())
        cna = record["containers"]["cna"]
        assert cna["title"] == "New title"
        assert cna["descriptions"][0]["value"] == "New impact."
        # Not ours to manage — an operator-added metric survives.
        assert cna["metrics"] == [{"cvssV3_1": {"baseScore": 7.5}}]

    def test_existing_is_not_mutated(self) -> None:
        existing = self._existing()
        report = SecurityReportFactory(title="New title", matched_cve_id="CVE-2026-12345")
        build_cve5_json(report, pmc="spark", existing=existing)
        assert existing["containers"]["cna"]["title"] == "Old title"
