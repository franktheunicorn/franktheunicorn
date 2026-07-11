"""Tests for security triage prompt construction."""

from __future__ import annotations

from franktheunicorn.security.prompt import build_parse_prompt, build_triage_prompt


class TestBuildParsePrompt:
    def test_returns_system_and_user(self) -> None:
        system, user = build_parse_prompt("Some vulnerability report text")
        assert isinstance(system, str)
        assert isinstance(user, str)

    def test_system_prompt_requests_json(self) -> None:
        system, _ = build_parse_prompt("text")
        assert "JSON" in system

    def test_user_message_contains_report(self) -> None:
        raw = "Buffer overflow in parse_input()"
        _, user = build_parse_prompt(raw)
        assert raw in user

    def test_system_prompt_specifies_fields(self) -> None:
        system, _ = build_parse_prompt("text")
        for field in ("title", "component", "poc", "impact", "severity"):
            assert field in system


class TestBuildTriagePrompt:
    def test_returns_system_and_user(self) -> None:
        system, user = build_triage_prompt(
            parsed_component="auth.py",
            parsed_poc="run the script",
            parsed_impact="RCE",
            project_context="",
        )
        assert isinstance(system, str)
        assert isinstance(user, str)

    def test_user_message_includes_fields(self) -> None:
        _, user = build_triage_prompt(
            parsed_component="shell_runner.py",
            parsed_poc="echo hello",
            parsed_impact="command injection",
            project_context="",
        )
        assert "shell_runner.py" in user
        assert "echo hello" in user
        assert "command injection" in user

    def test_project_context_included_when_provided(self) -> None:
        _, user = build_triage_prompt(
            parsed_component="x",
            parsed_poc="y",
            parsed_impact="z",
            project_context="This tool runs shell commands by design.",
        )
        assert "This tool runs shell commands by design." in user

    def test_no_context_message_when_empty(self) -> None:
        _, user = build_triage_prompt(
            parsed_component="x",
            parsed_poc="y",
            parsed_impact="z",
            project_context="",
        )
        assert "No project documentation available" in user

    def test_system_prompt_mentions_expected_behavior(self) -> None:
        system, _ = build_triage_prompt(
            parsed_component="x",
            parsed_poc="y",
            parsed_impact="z",
            project_context="",
        )
        assert "expected" in system.lower() or "documented" in system.lower()

    def test_system_prompt_mentions_trust_boundaries(self) -> None:
        system, _ = build_triage_prompt(
            parsed_component="x",
            parsed_poc="y",
            parsed_impact="z",
            project_context="",
        )
        lowered = system.lower()
        assert "security model" in lowered
        assert "trusted" in lowered
        # It should call out the input-channel distinction (data file vs code).
        assert "data file" in lowered

    def test_security_model_included_when_provided(self) -> None:
        _, user = build_triage_prompt(
            parsed_component="x",
            parsed_poc="y",
            parsed_impact="z",
            project_context="",
            security_model="Loaded models are trusted and may run arbitrary code.",
        )
        assert "Loaded models are trusted and may run arbitrary code." in user
        assert "Trust Boundaries" in user

    def test_security_model_suppresses_no_docs_message(self) -> None:
        """With a security model present, the 'no documentation' filler is
        pointless — the model has authoritative context."""
        _, user = build_triage_prompt(
            parsed_component="x",
            parsed_poc="y",
            parsed_impact="z",
            project_context="",
            security_model="Data files are untrusted input.",
        )
        assert "No project documentation available" not in user

    def test_cve_candidates_rendered(self) -> None:
        _, user = build_triage_prompt(
            parsed_component="x",
            parsed_poc="y",
            parsed_impact="z",
            project_context="",
            cve_candidates=[
                {
                    "cve_id": "CVE-2025-30065",
                    "description": "Parquet deserialization RCE",
                    "cvss_score": 10.0,
                    "status": "Analyzed",
                }
            ],
        )
        assert "CVE-2025-30065" in user
        assert "Parquet deserialization RCE" in user
        assert "Candidate CVE Matches" in user

    def test_cve_candidates_tolerate_bad_entries(self) -> None:
        """Malformed entries (not dicts) are skipped, not crashed on."""
        _, user = build_triage_prompt(
            parsed_component="x",
            parsed_poc="y",
            parsed_impact="z",
            project_context="",
            cve_candidates=["not a dict", {"cve_id": "CVE-2024-1"}],
        )
        assert "CVE-2024-1" in user
