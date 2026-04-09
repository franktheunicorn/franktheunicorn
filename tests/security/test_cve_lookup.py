"""Tests for CVE lookup via NVD API."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from franktheunicorn.security.cve_lookup import (
    CVEMatch,
    _extract_cvss_score,
    _parse_nvd_response,
    search_cves,
)


class TestCVEMatch:
    def test_to_dict(self) -> None:
        match = CVEMatch(
            cve_id="CVE-2024-1234",
            description="Buffer overflow in parser",
            cvss_score=7.5,
            status="Analyzed",
            published="2024-01-15T00:00:00",
        )
        d = match.to_dict()
        assert d["cve_id"] == "CVE-2024-1234"
        assert d["cvss_score"] == 7.5
        assert d["status"] == "Analyzed"


class TestParseNvdResponse:
    def test_empty_response(self) -> None:
        assert _parse_nvd_response({}) == []

    def test_no_vulnerabilities(self) -> None:
        assert _parse_nvd_response({"vulnerabilities": []}) == []

    def test_parses_cve_entry(self) -> None:
        data = {
            "vulnerabilities": [
                {
                    "cve": {
                        "id": "CVE-2024-5678",
                        "descriptions": [{"lang": "en", "value": "SQL injection vulnerability"}],
                        "vulnStatus": "Analyzed",
                        "published": "2024-06-01T00:00:00",
                        "metrics": {
                            "cvssMetricV31": [
                                {
                                    "cvssData": {"baseScore": 9.8},
                                }
                            ]
                        },
                    }
                }
            ]
        }
        matches = _parse_nvd_response(data)
        assert len(matches) == 1
        assert matches[0].cve_id == "CVE-2024-5678"
        assert matches[0].cvss_score == 9.8
        assert "SQL injection" in matches[0].description

    def test_skips_non_english_descriptions(self) -> None:
        data = {
            "vulnerabilities": [
                {
                    "cve": {
                        "id": "CVE-2024-0001",
                        "descriptions": [
                            {"lang": "es", "value": "Descripcion en espanol"},
                            {"lang": "en", "value": "English description"},
                        ],
                        "vulnStatus": "Modified",
                        "published": "2024-01-01",
                        "metrics": {},
                    }
                }
            ]
        }
        matches = _parse_nvd_response(data)
        assert matches[0].description == "English description"

    def test_handles_missing_metrics(self) -> None:
        data = {
            "vulnerabilities": [
                {
                    "cve": {
                        "id": "CVE-2024-0002",
                        "descriptions": [{"lang": "en", "value": "test"}],
                        "vulnStatus": "Rejected",
                        "published": "2024-01-01",
                    }
                }
            ]
        }
        matches = _parse_nvd_response(data)
        assert matches[0].cvss_score is None


class TestExtractCvssScore:
    def test_prefers_v31(self) -> None:
        metrics = {
            "cvssMetricV31": [{"cvssData": {"baseScore": 9.0}}],
            "cvssMetricV30": [{"cvssData": {"baseScore": 8.0}}],
        }
        assert _extract_cvss_score(metrics) == 9.0

    def test_falls_back_to_v30(self) -> None:
        metrics = {"cvssMetricV30": [{"cvssData": {"baseScore": 7.5}}]}
        assert _extract_cvss_score(metrics) == 7.5

    def test_falls_back_to_v2(self) -> None:
        metrics = {"cvssMetricV2": [{"cvssData": {"baseScore": 6.0}}]}
        assert _extract_cvss_score(metrics) == 6.0

    def test_returns_none_for_empty(self) -> None:
        assert _extract_cvss_score({}) is None

    def test_returns_none_for_non_dict(self) -> None:
        assert _extract_cvss_score("not a dict") is None


class TestSearchCves:
    def test_empty_keyword_returns_empty(self) -> None:
        assert search_cves("") == []
        assert search_cves("  ") == []

    def test_successful_search(self) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "vulnerabilities": [
                {
                    "cve": {
                        "id": "CVE-2024-9999",
                        "descriptions": [{"lang": "en", "value": "Test vuln"}],
                        "vulnStatus": "Analyzed",
                        "published": "2024-01-01",
                        "metrics": {},
                    }
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_response

        matches = search_cves("test", http_client=mock_client)
        assert len(matches) == 1
        assert matches[0].cve_id == "CVE-2024-9999"

    def test_rate_limited_returns_empty(self) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 403

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_response

        matches = search_cves("test", http_client=mock_client)
        assert matches == []

    def test_http_error_returns_empty(self) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error",
            request=MagicMock(),
            response=mock_response,
        )

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = mock_response

        matches = search_cves("test", http_client=mock_client)
        assert matches == []
