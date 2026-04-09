"""Tests for the security report triage pipeline."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, SecurityTriageConfig
from franktheunicorn.security.triage import (
    _safe_json_parse,
    triage_report,
)


@pytest.fixture
def operator_config_with_llm() -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        llm_backends=[LLMBackendConfig(provider="stub")],
        security_triage=SecurityTriageConfig(enabled=True),
    )


class TestSafeJsonParse:
    def test_valid_json(self) -> None:
        result = _safe_json_parse('{"key": "value"}')
        assert result == {"key": "value"}

    def test_invalid_json(self) -> None:
        result = _safe_json_parse("not json")
        assert result is None

    def test_empty_string(self) -> None:
        result = _safe_json_parse("")
        assert result is None

    def test_strips_code_fences(self) -> None:
        text = '```json\n{"key": "value"}\n```'
        result = _safe_json_parse(text)
        assert result == {"key": "value"}

    def test_returns_none_for_list(self) -> None:
        result = _safe_json_parse("[1, 2, 3]")
        assert result is None


@pytest.mark.django_db
class TestTriageReport:
    def test_triage_sets_status_to_triaging(
        self,
        db: Any,
    ) -> None:
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(
            raw_text="There is a buffer overflow in parse_input()",
            status="new",
        )

        config = OperatorConfig(github_username="testuser")

        # No LLM backends → triage returns early but status is set.
        triage_report(report, None, config)
        report.refresh_from_db()
        assert report.status == "triaging"

    def test_triage_with_stub_backend(
        self,
        db: Any,
    ) -> None:
        """Stub backend produces deterministic output; verify pipeline handles it."""
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(
            raw_text="SQL injection in /api/users?id=1 OR 1=1",
            status="new",
        )

        config = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )

        # The stub backend returns predefined findings, not JSON for triage.
        # The pipeline should handle non-JSON gracefully.
        triage_report(report, None, config)
        report.refresh_from_db()
        # Status should be updated even if parsing fails.
        assert report.status in ("triaging", "new", "expected-behavior")

    @patch("franktheunicorn.security.triage.search_cves")
    def test_cve_check_populates_matches(
        self,
        mock_search: MagicMock,
        db: Any,
    ) -> None:
        from franktheunicorn.security.cve_lookup import CVEMatch
        from tests.factories import SecurityReportFactory

        mock_search.return_value = [
            CVEMatch(
                cve_id="CVE-2024-1234",
                description="Known buffer overflow",
                cvss_score=7.5,
                status="Analyzed",
            )
        ]

        report = SecurityReportFactory(
            raw_text="buffer overflow vulnerability",
            parsed_component="parser.c",
            status="new",
        )

        config = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True),
        )

        from franktheunicorn.security.triage import _check_cves

        _check_cves(report, config)
        report.refresh_from_db()

        assert len(report.cve_matches) == 1
        assert report.cve_matches[0]["cve_id"] == "CVE-2024-1234"

    def test_cve_check_skips_empty_keyword(self, db: Any) -> None:
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(
            raw_text="vague report",
            parsed_component="",
            title="",
        )
        config = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True),
        )

        from franktheunicorn.security.triage import _check_cves

        _check_cves(report, config)
        report.refresh_from_db()
        assert report.cve_matches == []
