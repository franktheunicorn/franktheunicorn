"""Tests for the security-context sub-check."""

from __future__ import annotations

import json

from franktheunicorn.review.checks.security_context import SecurityContextCheck
from tests.conftest import make_pr_context


class TestSecurityContextCheckPrompt:
    def test_system_prompt_focuses_on_applied_context(self) -> None:
        ctx = make_pr_context()
        check = SecurityContextCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "existing codebase" in system.lower()
        assert "trust boundaries" in system.lower()
        assert "weaken" in system.lower()

    def test_system_prompt_excludes_direct_vulnerability_analysis(self) -> None:
        ctx = make_pr_context()
        check = SecurityContextCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "Do NOT look for vulnerabilities introduced directly by new code" in system

    def test_system_prompt_includes_schema(self) -> None:
        ctx = make_pr_context()
        check = SecurityContextCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "findings" in system
        assert "file_path" in system

    def test_system_prompt_excludes_non_security_concerns(self) -> None:
        ctx = make_pr_context()
        check = SecurityContextCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "Do NOT comment on code style" in system

    def test_user_message_delegates_to_build_user_message(self) -> None:
        diff = "+++ b/settings.py\n-CSRF_MIDDLEWARE = True\n+# CSRF_MIDDLEWARE = True"
        ctx = make_pr_context(pr_number=77, pr_title="Disable CSRF")
        check = SecurityContextCheck()
        _system, user = check.build_prompt(diff, ctx)
        assert "PR #77" in user
        assert "Disable CSRF" in user
        assert "CSRF_MIDDLEWARE" in user
        assert "```diff" in user


class TestSecurityContextCheckName:
    def test_name_is_security_context(self) -> None:
        assert SecurityContextCheck.name == "security-context"
        assert SecurityContextCheck().name == "security-context"


class TestSecurityContextCheckParsing:
    def test_parse_contextual_finding(self) -> None:
        check = SecurityContextCheck()
        raw = json.dumps(
            {
                "findings": [
                    {
                        "file_path": "src/middleware.py",
                        "line_number": 15,
                        "title": "security-context: CSRF middleware removed",
                        "body": "Removing the CSRF middleware weakens the app's security.",
                        "confidence": 0.85,
                        "severity": "critical",
                    }
                ]
            }
        )
        findings = check.parse_response(raw)
        assert len(findings) == 1
        assert findings[0].file_path == "src/middleware.py"
        assert findings[0].severity == "critical"
        assert findings[0].title.startswith("security-context:")

    def test_parse_empty_findings(self) -> None:
        check = SecurityContextCheck()
        findings = check.parse_response('{"findings": []}')
        assert findings == []

    def test_parse_invalid_json(self) -> None:
        check = SecurityContextCheck()
        findings = check.parse_response("not json at all")
        assert findings == []
