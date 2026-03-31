"""Tests for the coverage sub-check."""

from __future__ import annotations

import json

from franktheunicorn.review.checks.coverage import CoverageCheck
from tests.conftest import make_pr_context


class TestCoverageCheckPrompt:
    def test_system_prompt_includes_schema(self) -> None:
        ctx = make_pr_context()
        check = CoverageCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "test-coverage" in system
        assert "findings" in system
        assert "file_path" in system  # schema fields present

    def test_system_prompt_includes_test_expectations(self) -> None:
        ctx = make_pr_context(test_expectations="all new features must have tests")
        check = CoverageCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "all new features must have tests" in system

    def test_system_prompt_handles_empty_test_expectations(self) -> None:
        ctx = make_pr_context(test_expectations="")
        check = CoverageCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "test-coverage" in system

    def test_user_message_delegates_to_build_user_message(self) -> None:
        """User message is built by the shared build_user_message helper."""
        diff = "+++ b/main.py\n+def new_func():\n+    pass"
        ctx = make_pr_context(pr_number=99, pr_title="Add widget")
        check = CoverageCheck()
        _system, user = check.build_prompt(diff, ctx)
        assert "PR #99" in user
        assert "Add widget" in user
        assert "new_func" in user
        assert "```diff" in user


class TestCoverageCheckName:
    def test_name_is_coverage(self) -> None:
        assert CoverageCheck.name == "coverage"
        assert CoverageCheck().name == "coverage"


class TestCoverageCheckParsing:
    def test_parse_valid_findings(self) -> None:
        check = CoverageCheck()
        raw = json.dumps(
            {
                "findings": [
                    {
                        "file_path": "src/app.py",
                        "line_number": 15,
                        "title": "test-coverage: untested error path",
                        "body": "The except branch on line 15 is not covered.",
                        "confidence": 0.7,
                        "severity": "important",
                    }
                ]
            }
        )
        findings = check.parse_response(raw)
        assert len(findings) == 1
        assert findings[0].file_path == "src/app.py"
        assert findings[0].severity == "important"

    def test_parse_empty_findings(self) -> None:
        check = CoverageCheck()
        findings = check.parse_response('{"findings": []}')
        assert findings == []

    def test_parse_invalid_json(self) -> None:
        check = CoverageCheck()
        findings = check.parse_response("not json at all")
        assert findings == []

    def test_parse_markdown_wrapped(self) -> None:
        check = CoverageCheck()
        raw = '```json\n{"findings": [{"body": "needs test", "severity": "nit"}]}\n```'
        findings = check.parse_response(raw)
        assert len(findings) == 1
        assert findings[0].body == "needs test"
