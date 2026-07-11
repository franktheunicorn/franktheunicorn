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
        """No LLM backend → skip gracefully. The report must stay in "new";
        flipping it to "triaging" before the backend check stranded reports
        out of the queue forever (worker email auto-triage has no guard)."""
        from tests.factories import SecurityReportFactory

        report = SecurityReportFactory(
            raw_text="There is a buffer overflow in parse_input()",
            status="new",
        )

        config = OperatorConfig(github_username="testuser")

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
        # The pipeline should handle non-JSON gracefully.
        triage_report(report, None, config)
        report.refresh_from_db()
        # Status should be updated even if parsing fails.
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

        with patch("django.conf.settings.FRANK_REPOS_DIR", str(tmp_path), create=True):
            triage_report(report, project_config, config)

        _system, analyze_user = backend.calls[-1]
        assert "Data files are untrusted; loaded models are trusted." in analyze_user

    @patch("franktheunicorn.security.triage._analyze_report")
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

        def _capture(*args: Any, **kwargs: Any) -> None:
            captured["cve_candidates"] = kwargs.get("cve_candidates")

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
        with patch("django.conf.settings.FRANK_REPOS_DIR", str(tmp_path), create=True):
            assert _resolve_security_model(self._pc()) == "Data files are untrusted input."

    def test_autodiscovers_generic_threat_model_name(self, tmp_path: Path) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "acme" / "widget"
        repo.mkdir(parents=True)
        (repo / "THREAT_MODEL.md").write_text("Only authenticated clients are trusted.")
        with patch("django.conf.settings.FRANK_REPOS_DIR", str(tmp_path), create=True):
            assert _resolve_security_model(self._pc()) == "Only authenticated clients are trusted."

    def test_explicit_file_path_loads(self, tmp_path: Path) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "acme" / "widget"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "trust.md").write_text("Models are trusted artifacts.")
        pc = self._pc(security_model_file="docs/trust.md")
        with patch("django.conf.settings.FRANK_REPOS_DIR", str(tmp_path), create=True):
            assert _resolve_security_model(pc) == "Models are trusted artifacts."

    def test_inline_wins_over_repo_file(self, tmp_path: Path) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "acme" / "widget"
        (repo / ".frank").mkdir(parents=True)
        (repo / ".frank" / "security-model.md").write_text("FROM FILE")
        pc = self._pc(security_model="FROM INLINE")
        with patch("django.conf.settings.FRANK_REPOS_DIR", str(tmp_path), create=True):
            assert _resolve_security_model(pc) == "FROM INLINE"

    def test_explicit_path_cannot_escape_repo(self, tmp_path: Path) -> None:
        """A security_model_file must not read files outside the repo."""
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "acme" / "widget"
        repo.mkdir(parents=True)
        (tmp_path / "secret.md").write_text("SECRET")
        pc = self._pc(security_model_file="../../secret.md")
        with patch("django.conf.settings.FRANK_REPOS_DIR", str(tmp_path), create=True):
            assert _resolve_security_model(pc) == ""

    def test_no_file_present_returns_empty(self, tmp_path: Path) -> None:
        from franktheunicorn.security.triage import _resolve_security_model

        (tmp_path / "acme" / "widget").mkdir(parents=True)
        with patch("django.conf.settings.FRANK_REPOS_DIR", str(tmp_path), create=True):
            assert _resolve_security_model(self._pc()) == ""

    def test_works_for_an_arbitrary_non_spark_repo(self, tmp_path: Path) -> None:
        """Same mechanism, different owner/repo — proves it is not hardcoded."""
        from franktheunicorn.security.triage import _resolve_security_model

        repo = tmp_path / "someorg" / "someproject"
        repo.mkdir(parents=True)
        (repo / "SECURITY_MODEL.md").write_text("Trust boundaries for an arbitrary project.")
        pc = self._pc(owner="someorg", repo="someproject")
        with patch("django.conf.settings.FRANK_REPOS_DIR", str(tmp_path), create=True):
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

        with patch("django.conf.settings.FRANK_REPOS_DIR", str(tmp_path), create=True):
            result = _load_project_context(report, pc)

        assert "Authentication is off by default" in result
        assert "docs/security.md" in result
