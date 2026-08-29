"""Tests for the security report triage pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

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


class _CapturingBackend(_MockLLMBackend):
    """Mock backend that also records the (system, user) prompts it received."""

    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.calls: list[tuple[str, str]] = []

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        self.calls.append((system_prompt, user_message))
        return super()._call_api(system_prompt, user_message, api_key)


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
    def test_triage_without_backend_leaves_status_untouched(
        self,
        db: Any,
    ) -> None:
        """No LLM backend → the report stays in "new" and the run is a failure.

        Status first: flipping it to "triaging" before the backend check stranded
        reports out of the queue forever (worker email auto-triage has no guard).

        And it raises rather than returning, so the WorkerCommand lands "failed".
        Returning normally marked it "completed", which the report page renders as
        "the model's answer had nothing usable in it — re-running is worth a try"
        when in fact no model exists to call and re-running is worth nothing.
        """
        from franktheunicorn.security.triage import TriageIncompleteError
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(
            raw_text="There is a buffer overflow in parse_input()",
            status="new",
        )

        config = OperatorConfig(github_username="testuser")

        with pytest.raises(TriageIncompleteError, match="No LLM backend"):
            triage_report(report, None, config)
        report.refresh_from_db()
        assert report.status == "new"

    @patch("franktheunicorn.security.triage.search_cves", return_value=[])
    def test_triage_with_stub_backend(
        self,
        mock_cves: MagicMock,
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
        # Non-JSON means no verdict, which is now raised rather than returned:
        # the worker has to mark the command failed, or a re-triage would show
        # the previous run's verdict as this one's. The report still lands back
        # in a queue rather than being stranded in "triaging".
        from franktheunicorn.security.triage import TriageIncompleteError

        with pytest.raises(TriageIncompleteError):
            triage_report(report, None, config)
        report.refresh_from_db()
        assert report.status in ("triaging", "new", "expected-behavior")

    @patch("franktheunicorn.security.triage.search_cves")
    @patch("franktheunicorn.security.triage._get_triage_backend")
    def test_full_pipeline_with_mock_backend(
        self,
        mock_get_backend: MagicMock,
        mock_cves: MagicMock,
        db: Any,
    ) -> None:
        """Test the full triage pipeline end-to-end with a mock LLM backend."""
        import json

        from tests.factories import SecurityReportFactory

        mock_cves.return_value = []
        parse_json = json.dumps(
            {
                "title": "XSS in form",
                "component": "forms.py",
                "poc": "inject script",
                "impact": "XSS",
                "severity": "medium",
            }
        )
        analyze_json = json.dumps(
            {
                "poc_plausible": True,
                "poc_assessment": "Valid XSS.",
                "is_expected_behavior": False,
                "expected_behavior_explanation": "",
                "assessed_severity": "medium",
                "triage_summary": "Real XSS.",
            }
        )
        backend = _MockLLMBackend(responses=[parse_json, analyze_json])
        mock_get_backend.return_value = backend

        report = SecurityReportFactory(raw_text="XSS vuln", title="", status="new")
        config = OperatorConfig(
            github_username="testuser",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )

        triage_report(report, None, config)
        report.refresh_from_db()
        assert report.title == "XSS in form"
        assert report.poc_plausible is True
        assert report.status == "new"

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

        _parse_report(report, backend)

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

        _analyze_report(report, backend, "")

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

        _analyze_report(report, backend, "")

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

        # Should not raise.
        _parse_report(report, backend)

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

        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
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

        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
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

        # Should not raise.
        _analyze_report(report, backend, "")

        report.refresh_from_db()
        assert report.triage_summary == ""  # nothing was analyzed


@pytest.mark.django_db
class TestSecurityModelThreading:
    """The project security model and candidate CVEs must reach the analysis
    prompt — that context is what lets triage tell "we run arbitrary code by
    design" reports apart from real data-file findings."""

    def _analyze_json(self) -> str:
        import json

        return json.dumps(
            {
                "poc_plausible": False,
                "poc_assessment": "",
                "is_expected_behavior": True,
                "expected_behavior_explanation": "Models are trusted.",
                "assessed_severity": "informational",
                "triage_summary": "Expected under the project security model.",
            }
        )

    def test_analyze_report_includes_security_model_and_cves(self, db: Any) -> None:
        from franktheunicorn.security.triage import _analyze_report
        from tests.factories import SecurityReportFactory

        backend = _CapturingBackend(responses=[self._analyze_json()])
        report = SecurityReportFactory(
            parsed_component="ParquetFileFormat.scala", status="triaging"
        )

        _analyze_report(
            report,
            backend,
            "",
            security_model="Loaded models are trusted; data files are not.",
            cve_candidates=[{"cve_id": "CVE-2025-30065", "description": "Parquet RCE"}],
        )

        assert backend.calls, "backend was never called"
        _system, user = backend.calls[-1]
        assert "Loaded models are trusted; data files are not." in user
        assert "CVE-2025-30065" in user

    @patch("franktheunicorn.security.triage.search_cves", return_value=[])
    @patch("franktheunicorn.security.triage._get_triage_backend")
    def test_triage_report_threads_project_security_model(
        self,
        mock_get_backend: MagicMock,
        mock_cves: MagicMock,
        db: Any,
    ) -> None:
        import json

        from franktheunicorn.config.models import ProjectConfig
        from tests.factories import SecurityReportFactory

        parse_json = json.dumps(
            {
                "title": "RCE via ExternalCommandExecutor",
                "component": "SparkConnectPlanner.scala",
                "poc": "upload jar; ExecuteExternalCommand",
                "impact": "RCE",
                "severity": "critical",
            }
        )
        backend = _CapturingBackend(responses=[parse_json, self._analyze_json()])
        mock_get_backend.return_value = backend

        report = SecurityReportFactory(raw_text="RCE report", title="", status="new")
        project_config = ProjectConfig(
            owner="apache",
            repo="spark",
            security_model="Spark treats submitted code and runners as trusted.",
        )
        config = OperatorConfig(
            github_username="holdenk",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )

        triage_report(report, project_config, config)

        # The final LLM call is the analysis; it must carry the security model.
        assert len(backend.calls) >= 2
        _system, analyze_user = backend.calls[-1]
        assert "Spark treats submitted code and runners as trusted." in analyze_user

    @patch("franktheunicorn.security.triage.search_cves", return_value=[])
    @patch("franktheunicorn.security.triage._get_triage_backend")
    def test_triage_autoloads_security_model_from_repo(
        self,
        mock_get_backend: MagicMock,
        mock_cves: MagicMock,
        db: Any,
        tmp_path: Path,
    ) -> None:
        """End-to-end: with no inline security_model, triage picks up a
        conventional threat-model file committed to the repo."""
        import json

        from franktheunicorn.config.models import ProjectConfig
        from tests.factories import ProjectFactory, SecurityReportFactory

        repo = tmp_path / "acme" / "widget"
        repo.mkdir(parents=True)
        (repo / "THREAT_MODEL.md").write_text(
            "Data files are untrusted; loaded models are trusted."
        )

        parse_json = json.dumps({"title": "t", "component": "c", "poc": "p", "impact": "i"})
        backend = _CapturingBackend(responses=[parse_json, self._analyze_json()])
        mock_get_backend.return_value = backend

        project = ProjectFactory(owner="acme", repo="widget")
        report = SecurityReportFactory(project=project, raw_text="report", title="", status="new")
        project_config = ProjectConfig(owner="acme", repo="widget")  # no inline model
        config = OperatorConfig(
            github_username="holdenk",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )

        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
            triage_report(report, project_config, config)

        _system, analyze_user = backend.calls[-1]
        assert "Data files are untrusted; loaded models are trusted." in analyze_user

    @patch("franktheunicorn.security.triage._analyze_report", return_value=(True, ""))
    @patch("franktheunicorn.security.triage.search_cves", return_value=[])
    @patch("franktheunicorn.security.triage._get_triage_backend")
    def test_cve_lookup_runs_before_analysis(
        self,
        mock_get_backend: MagicMock,
        mock_cves: MagicMock,
        mock_analyze: MagicMock,
        db: Any,
    ) -> None:
        """CVE matches must be populated before analysis so they can inform
        the expected-behavior / duplicate call."""
        import json

        from tests.factories import SecurityReportFactory

        parse_json = json.dumps({"title": "t", "component": "c", "poc": "p", "impact": "i"})
        backend = _MockLLMBackend(responses=[parse_json])
        mock_get_backend.return_value = backend

        from franktheunicorn.security.cve_lookup import CVEMatch

        mock_cves.return_value = [CVEMatch(cve_id="CVE-2025-30065", description="Parquet RCE")]

        # When _analyze_report is called, cve_candidates must already be filled.
        captured: dict[str, Any] = {}

        def _capture(*args: Any, **kwargs: Any) -> tuple[bool, str]:
            captured["cve_candidates"] = kwargs.get("cve_candidates")
            # (wrote_a_verdict, reason_it_did_not)
            return True, ""

        mock_analyze.side_effect = _capture

        report = SecurityReportFactory(raw_text="parquet vuln", title="", status="new")
        config = OperatorConfig(
            github_username="holdenk",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )

        triage_report(report, None, config)

        assert captured.get("cve_candidates"), "analysis ran before CVE lookup populated matches"
        assert captured["cve_candidates"][0]["cve_id"] == "CVE-2025-30065"


class TestResolveSecurityModel:
    """The security model is resolved dynamically for ANY repo — nothing is
    Spark-specific. Precedence: inline prose > explicit file > auto-discovery."""

    def _pc(self, owner: str = "acme", repo: str = "widget", **kw: Any) -> Any:
        from franktheunicorn.config.models import ProjectConfig

        return ProjectConfig(owner=owner, repo=repo, **kw)

    def test_none_config_returns_empty(self) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        assert _resolve_security_model(None) == ""

    def test_inline_prose_wins_without_repo(self) -> None:
        """Inline prose short-circuits before any repo lookup."""
        from franktheunicorn.security.triage import _resolve_security_model

        pc = self._pc(security_model="Submitted code is trusted.")
        assert _resolve_security_model(pc) == "Submitted code is trusted."

    def test_autodiscovers_dotfrank_file(self, tmp_path: Path) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "acme" / "widget"
        (repo / ".frank").mkdir(parents=True)
        (repo / ".frank" / "security-model.md").write_text("Data files are untrusted input.")
        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
            assert _resolve_security_model(self._pc()) == "Data files are untrusted input."

    def test_autodiscovers_generic_threat_model_name(self, tmp_path: Path) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "acme" / "widget"
        repo.mkdir(parents=True)
        (repo / "THREAT_MODEL.md").write_text("Only authenticated clients are trusted.")
        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
            assert _resolve_security_model(self._pc()) == "Only authenticated clients are trusted."

    def test_explicit_file_path_loads(self, tmp_path: Path) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "acme" / "widget"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "trust.md").write_text("Models are trusted artifacts.")
        pc = self._pc(security_model_file="docs/trust.md")
        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
            assert _resolve_security_model(pc) == "Models are trusted artifacts."

    def test_inline_wins_over_repo_file(self, tmp_path: Path) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "acme" / "widget"
        (repo / ".frank").mkdir(parents=True)
        (repo / ".frank" / "security-model.md").write_text("FROM FILE")
        pc = self._pc(security_model="FROM INLINE")
        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
            assert _resolve_security_model(pc) == "FROM INLINE"

    def test_explicit_path_cannot_escape_repo(self, tmp_path: Path) -> None:
        """A security_model_file must not read files outside the repo."""
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "acme" / "widget"
        repo.mkdir(parents=True)
        (tmp_path / "secret.md").write_text("SECRET")
        pc = self._pc(security_model_file="../../secret.md")
        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
            assert _resolve_security_model(pc) == ""

    def test_no_file_present_returns_empty(self, tmp_path: Path) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        (tmp_path / "acme" / "widget").mkdir(parents=True)
        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
            assert _resolve_security_model(self._pc()) == ""

    def test_works_for_an_arbitrary_non_spark_repo(self, tmp_path: Path) -> None:
        """Same mechanism, different owner/repo — proves it is not hardcoded."""
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "someorg" / "someproject"
        repo.mkdir(parents=True)
        (repo / "SECURITY_MODEL.md").write_text("Trust boundaries for an arbitrary project.")
        pc = self._pc(owner="someorg", repo="someproject")
        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
            assert "arbitrary project" in _resolve_security_model(pc)


@pytest.mark.django_db
class TestSecurityDocContext:
    def test_load_project_context_reads_security_doc(self, db: Any, tmp_path: Path) -> None:
        """docs/security.md (where Spark and many projects keep their security
        posture) is pulled into triage context for any repo."""
        from franktheunicorn.config.models import ProjectConfig
        from franktheunicorn.security.triage import _load_project_context
        from tests.factories import SecurityReportFactory

        repo_dir = tmp_path / "testorg" / "testrepo"
        (repo_dir / "docs").mkdir(parents=True)
        (repo_dir / "docs" / "security.md").write_text(
            "Authentication is off by default; secure your cluster."
        )
        report = SecurityReportFactory(raw_text="test")
        pc = ProjectConfig(owner="testorg", repo="testrepo")

        with override_settings(FRANK_REPOS_DIR=str(tmp_path)):
            result = _load_project_context(report, pc)

        assert "Authentication is off by default" in result
        assert "docs/security.md" in result


class TestTriStateCoercion:
    """A hedge must not become a verdict, and 'yes' must not become 'no'."""

    def test_affirmative_words_are_true(self) -> None:
        from franktheunicorn.security.triage import _coerce_tristate

        for word in ("true", "TRUE", "yes", " Yes ", "plausible", "confirmed", "likely", "1"):
            assert _coerce_tristate(word) is True, word

    def test_negative_words_are_false(self) -> None:
        from franktheunicorn.security.triage import _coerce_tristate

        for word in ("false", "no", "not plausible", "implausible", "unlikely", "0"):
            assert _coerce_tristate(word) is False, word

    def test_a_hedge_is_none_not_false(self) -> None:
        """'unknown' -> False published a green "POC: Not Plausible"."""
        from franktheunicorn.security.triage import _coerce_tristate

        for word in ("unknown", "unclear", "maybe", "n/a", "", "who knows"):
            assert _coerce_tristate(word) is None, word

    def test_yes_used_to_read_as_not_plausible(self) -> None:
        """The regression that mattered most: an affirmative shown as a negative."""
        from franktheunicorn.security.triage import _coerce_tristate

        assert _coerce_tristate("yes") is True

    def test_booleans_and_none_pass_through(self) -> None:
        from franktheunicorn.security.triage import _coerce_tristate

        assert _coerce_tristate(True) is True
        assert _coerce_tristate(False) is False
        assert _coerce_tristate(None) is None

    def test_the_non_nullable_helper_stays_conservative(self) -> None:
        """is_expected_behavior has no null; "didn't say" must not mean "expected"."""
        from franktheunicorn.security.triage import _coerce_bool

        assert _coerce_bool("unknown") is False
        assert _coerce_bool("yes") is True


@pytest.mark.django_db
class TestPartialFailureIsolation:
    """Optional context and unrecognised answers must not destroy a good verdict."""

    def _config(self) -> OperatorConfig:
        return OperatorConfig(
            github_username="testuser", llm_backends=[LLMBackendConfig(provider="stub")]
        )

    @patch("franktheunicorn.security.triage._get_triage_backend")
    def test_a_wrong_key_answer_does_not_blank_a_previous_verdict(
        self, mock_get_backend: MagicMock, db: Any
    ) -> None:
        """`if analysis:` only asked whether the dict was non-empty.

        So {"assessment": "..."} — a wrong key name, entirely ordinary — counted
        as a verdict, the command landed "completed", and the five fields were
        overwritten with blanks. On a re-triage that destroys the run before it.
        """
        import json

        from franktheunicorn.security.triage import TriageIncompleteError
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(
            raw_text="A path traversal vulnerability with an exploit.",
            triage_summary="Previously judged real by a good run.",
            poc_plausible=True,
            poc_assessment="Worked when I tried it.",
            status="new",
        )
        parse_json = json.dumps({"title": "t", "component": "c", "severity": "high"})
        mock_get_backend.return_value = _MockLLMBackend(
            responses=[parse_json, json.dumps({"assessment": "looks real to me"})]
        )

        with (
            patch("franktheunicorn.security.triage._check_cves"),
            pytest.raises(TriageIncompleteError),
        ):
            triage_report(report, None, self._config())

        report.refresh_from_db()
        assert report.triage_summary == "Previously judged real by a good run."
        assert report.poc_plausible is True

    @patch("franktheunicorn.security.triage._get_triage_backend")
    def test_a_failed_cve_lookup_does_not_abort_the_run(
        self, mock_get_backend: MagicMock, db: Any
    ) -> None:
        """NVD answers 200 with an HTML maintenance page; .json() raises ValueError.

        search_cves catches only httpx errors, so that escaped and took the run
        down after the parse call was billed and before the verdict call.
        """
        import json

        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(raw_text="A vulnerability with an exploit.", status="new")
        parse_json = json.dumps({"title": "t", "component": "c", "severity": "high"})
        analyze_json = json.dumps(
            {"poc_plausible": True, "poc_assessment": "yes", "triage_summary": "Real."}
        )
        mock_get_backend.return_value = _MockLLMBackend(responses=[parse_json, analyze_json])

        with patch(
            "franktheunicorn.security.triage._check_cves",
            side_effect=ValueError("Expecting value: line 1 column 1"),
        ):
            triage_report(report, None, self._config())

        report.refresh_from_db()
        assert report.triage_summary == "Real."

    @patch("franktheunicorn.security.triage._get_triage_backend", return_value=None)
    def test_no_backend_unsticks_a_report_left_in_triaging(
        self, _mock_backend: MagicMock, db: Any
    ) -> None:
        """The raise returns early, so the try/finally never ran.

        A report stranded in "triaging" by an earlier killed run stayed there,
        invisible in the new queue, with its command now failed.
        """
        from franktheunicorn.security.triage import TriageIncompleteError
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(raw_text="A vulnerability.", status="triaging")

        with pytest.raises(TriageIncompleteError):
            triage_report(report, None, OperatorConfig(github_username="testuser"))

        report.refresh_from_db()
        assert report.status == "new"


@pytest.mark.django_db
class TestTriageSaysWhyItHadNoVerdict:
    """ "Triage produced no verdict for report #2" named none of the three causes."""

    def _report(self) -> Any:
        from tests.factories import SecurityReportFactory

        return SecurityReportFactory(raw_text="vuln", parsed_component="test.py", status="triaging")

    def test_an_unreachable_backend_is_named_and_gets_no_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import httpx

        from franktheunicorn.security.triage import _analyze_report

        backend = _MockLLMBackend(responses=[])
        try:
            httpx.get("http://127.0.0.1:1/api/generate", timeout=2)
        except Exception as exc:
            backend._call_api = MagicMock(side_effect=exc)  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            wrote, reason = _analyze_report(self._report(), backend, "")

        assert wrote is False
        assert "not reachable" in reason
        record = next(r for r in caplog.records if "could not reach the LLM backend" in r.message)
        assert record.levelno == logging.WARNING
        assert record.exc_info is None

    def test_a_reply_with_no_usable_fields_says_so(self) -> None:
        import json

        from franktheunicorn.security.triage import _analyze_report

        backend = _MockLLMBackend(responses=[json.dumps({"totally": "unrelated"})])
        wrote, reason = _analyze_report(self._report(), backend, "")

        assert wrote is False
        assert "without any of the fields triage reads" in reason

    def test_the_reason_reaches_the_operator_visible_error(self) -> None:
        from franktheunicorn.security.triage import TriageIncompleteError, triage_report

        report = self._report()
        config = OperatorConfig(
            github_username="holdenk",
            llm_backends=[LLMBackendConfig(provider="stub")],
            security_triage=SecurityTriageConfig(enabled=True),
        )
        with (
            patch(
                "franktheunicorn.security.triage._analyze_report",
                return_value=(False, "the LLM backend is not reachable (Connection refused)"),
            ),
            patch("franktheunicorn.security.triage.search_cves", return_value=[]),
            pytest.raises(TriageIncompleteError, match="not reachable"),
        ):
            triage_report(report, None, config)
