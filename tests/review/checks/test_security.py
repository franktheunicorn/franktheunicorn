"""Tests for the security sub-check."""

from __future__ import annotations

import json

from franktheunicorn.review.checks.security import SecurityCheck
from tests.conftest import make_pr_context


class TestSecurityCheckPrompt:
    def test_system_prompt_includes_schema(self) -> None:
        ctx = make_pr_context()
        check = SecurityCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "security" in system
        assert "findings" in system
        assert "file_path" in system  # schema fields present

    def test_system_prompt_includes_vulnerability_categories(self) -> None:
        ctx = make_pr_context()
        check = SecurityCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "injection" in system.lower()
        assert "OWASP" in system

    def test_system_prompt_excludes_non_security_concerns(self) -> None:
        ctx = make_pr_context()
        check = SecurityCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "Do NOT comment on code style" in system

    def test_user_message_delegates_to_build_user_message(self) -> None:
        """User message is built by the shared build_user_message helper."""
        diff = "+++ b/main.py\n+password = 'hardcoded'\n+    pass"
        ctx = make_pr_context(pr_number=99, pr_title="Add auth")
        check = SecurityCheck()
        _system, user = check.build_prompt(diff, ctx)
        assert "PR #99" in user
        assert "Add auth" in user
        assert "hardcoded" in user
        assert "```diff" in user


class TestSecurityCheckName:
    def test_name_is_security(self) -> None:
        assert SecurityCheck.name == "security"
        assert SecurityCheck().name == "security"


class TestSecurityCheckParsing:
    def test_parse_valid_findings(self) -> None:
        check = SecurityCheck()
        raw = json.dumps(
            {
                "findings": [
                    {
                        "file_path": "src/auth.py",
                        "line_number": 42,
                        "title": "security: hardcoded secret",
                        "body": "API key is hardcoded on line 42.",
                        "confidence": 0.9,
                        "severity": "critical",
                    }
                ]
            }
        )
        findings = check.parse_response(raw)
        assert len(findings) == 1
        assert findings[0].file_path == "src/auth.py"
        assert findings[0].severity == "critical"

    def test_parse_empty_findings(self) -> None:
        check = SecurityCheck()
        findings = check.parse_response('{"findings": []}')
        assert findings == []

    def test_parse_invalid_json(self) -> None:
        check = SecurityCheck()
        findings = check.parse_response("not json at all")
        assert findings == []

    def test_parse_markdown_wrapped(self) -> None:
        check = SecurityCheck()
        raw = '```json\n{"findings": [{"body": "SQL injection risk", "severity": "critical"}]}\n```'
        findings = check.parse_response(raw)
        assert len(findings) == 1
        assert findings[0].body == "SQL injection risk"
