"""Tests for the security report triage pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, SecurityTriageConfig
from franktheunicorn.review.backends.base import BaseLLMBackend
from franktheunicorn.security.triage import (
    _safe_json_parse,
    triage_report,
)


class _MockLLMBackend(BaseLLMBackend):
    """Test backend that returns canned responses."""

    _sdk_module = ""
    _default_key_env = ""
    _default_model = ""

    def __init__(self, responses: list[str]) -> None:
        super().__init__(LLMBackendConfig(provider="stub"))
        self._model = "test"
        self._responses = responses
        self._call_count = 0

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[idx]


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

    def test_parse_report_populates_fields(self, db: Any) -> None:
        """Test that _parse_report populates structured fields from LLM JSON."""
        import json

        from franktheunicorn.security.triage import _parse_report
        from tests.factories import SecurityReportFactory

        parse_response = json.dumps(
            {
                "title": "Buffer overflow in parser",
                "component": "src/parser.c",
                "poc": "Run: ./exploit --target parser",
                "impact": "Remote code execution",
                "severity": "high",
                "reporter_name": "Alice",
                "reporter_email": "alice@example.com",
            }
        )

        backend = _MockLLMBackend(responses=[parse_response])
        report = SecurityReportFactory(raw_text="buffer overflow", title="", status="new")
        config = OperatorConfig(github_username="testuser")

        _parse_report(report, backend, config)

        report.refresh_from_db()
        assert report.title == "Buffer overflow in parser"
        assert report.parsed_component == "src/parser.c"
        assert report.parsed_poc == "Run: ./exploit --target parser"
        assert report.parsed_impact == "Remote code execution"
        assert report.assessed_severity == "high"
        assert report.reporter_name == "Alice"

    def test_analyze_report_expected_behavior(self, db: Any) -> None:
        """Test that _analyze_report detects expected behavior."""
        import json

        from franktheunicorn.security.triage import _analyze_report
        from tests.factories import SecurityReportFactory

        analyze_response = json.dumps(
            {
                "poc_plausible": False,
                "poc_assessment": "Documented purpose of the tool.",
                "is_expected_behavior": True,
                "expected_behavior_explanation": "The tool runs commands by design.",
                "assessed_severity": "informational",
                "triage_summary": "Not a vulnerability.",
            }
        )

        backend = _MockLLMBackend(responses=[analyze_response])
        report = SecurityReportFactory(
            raw_text="shell runs commands",
            parsed_component="shell.py",
            parsed_poc="shell --exec ls",
            parsed_impact="command execution",
            status="triaging",
        )
        config = OperatorConfig(github_username="testuser")

        _analyze_report(report, backend, "", config)

        report.refresh_from_db()
        assert report.is_expected_behavior is True
        assert report.status == "expected-behavior"
        assert "by design" in report.expected_behavior_explanation

    def test_analyze_report_plausible_poc(self, db: Any) -> None:
        """Test that a plausible POC keeps status as 'new'."""
        import json

        from franktheunicorn.security.triage import _analyze_report
        from tests.factories import SecurityReportFactory

        analyze_response = json.dumps(
            {
                "poc_plausible": True,
                "poc_assessment": "Real buffer overflow.",
                "is_expected_behavior": False,
                "expected_behavior_explanation": "",
                "assessed_severity": "high",
                "triage_summary": "Legitimate vulnerability.",
            }
        )

        backend = _MockLLMBackend(responses=[analyze_response])
        report = SecurityReportFactory(
            raw_text="overflow",
            parsed_component="parser.c",
            status="triaging",
        )
        config = OperatorConfig(github_username="testuser")

        _analyze_report(report, backend, "", config)

        report.refresh_from_db()
        assert report.poc_plausible is True
        assert report.status == "new"
        assert report.assessed_severity == "high"

    def test_parse_report_api_error_handled(self, db: Any) -> None:
        """Test that an LLM API error in _parse_report doesn't crash."""
        from franktheunicorn.security.triage import _parse_report
        from tests.factories import SecurityReportFactory

        backend = _MockLLMBackend(responses=[])
        backend._call_api = MagicMock(side_effect=RuntimeError("API down"))  # type: ignore[method-assign]

        report = SecurityReportFactory(raw_text="vuln", status="new")
        config = OperatorConfig(github_username="testuser")

        # Should not raise.
        _parse_report(report, backend, config)

        report.refresh_from_db()
        assert report.parsed_component == ""  # nothing was parsed

    def test_load_project_context_no_repo(self, db: Any) -> None:
        """Test _load_project_context returns empty when no repo exists."""
        from franktheunicorn.config.models import ProjectConfig
        from franktheunicorn.security.triage import _load_project_context
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(raw_text="test")
        pc = ProjectConfig(owner="test", repo="nonexistent")
        result = _load_project_context(report, pc)
        assert result == ""

    def test_load_project_context_none_config(self, db: Any) -> None:
        """Test _load_project_context returns empty with None config."""
        from franktheunicorn.security.triage import _load_project_context
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(raw_text="test")
        assert _load_project_context(report, None) == ""

    def test_load_project_context_reads_files(self, db: Any, tmp_path: Path) -> None:
        """Test _load_project_context reads README, SECURITY.md, and component."""
        from franktheunicorn.config.models import ProjectConfig
        from franktheunicorn.security.triage import _load_project_context
        from tests.factories import SecurityReportFactory

        # Create fake repo structure.
        repo_dir = tmp_path / "testorg" / "testrepo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("# Test Project\nThis is a test.")
        (repo_dir / "SECURITY.md").write_text("# Security Policy\nReport to security@.")
        (repo_dir / "src").mkdir()
        (repo_dir / "src" / "parser.py").write_text("def parse(): pass")

        report = SecurityReportFactory(
            raw_text="test",
            parsed_component="src/parser.py",
        )
        pc = ProjectConfig(owner="testorg", repo="testrepo")

        with patch(
            "django.conf.settings.FRANK_REPOS_DIR",
            str(tmp_path),
            create=True,
        ):
            result = _load_project_context(report, pc)

        assert "# Test Project" in result
        assert "Security Policy" in result
        assert "def parse()" in result

    def test_load_project_context_handles_read_error(self, db: Any, tmp_path: Path) -> None:
        """Test _load_project_context logs and continues on OSError."""
        from franktheunicorn.config.models import ProjectConfig
        from franktheunicorn.security.triage import _load_project_context
        from tests.factories import SecurityReportFactory

        repo_dir = tmp_path / "testorg" / "testrepo"
        repo_dir.mkdir(parents=True)
        readme = repo_dir / "README.md"
        readme.write_text("content")
        readme.chmod(0o000)  # make unreadable

        report = SecurityReportFactory(raw_text="test")
        pc = ProjectConfig(owner="testorg", repo="testrepo")

        with patch(
            "django.conf.settings.FRANK_REPOS_DIR",
            str(tmp_path),
            create=True,
        ):
            result = _load_project_context(report, pc)

        # Should not crash, returns whatever it could read.
        assert isinstance(result, str)
        # Clean up permissions so tmp_path cleanup works.
        readme.chmod(0o644)

    def test_analyze_report_api_error_handled(self, db: Any) -> None:
        """Test that an LLM API error in _analyze_report doesn't crash."""
        from franktheunicorn.security.triage import _analyze_report
        from tests.factories import SecurityReportFactory

        backend = _MockLLMBackend(responses=[])
        backend._call_api = MagicMock(side_effect=RuntimeError("API down"))  # type: ignore[method-assign]

        report = SecurityReportFactory(
            raw_text="vuln",
            parsed_component="test.py",
            status="triaging",
        )
        config = OperatorConfig(github_username="testuser")

        # Should not raise.
        _analyze_report(report, backend, "", config)

        report.refresh_from_db()
        assert report.triage_summary == ""  # nothing was analyzed
