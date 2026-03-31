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
        # Should not crash; expectations section is just empty
        assert "test-coverage" in system

    def test_user_message_includes_diff(self) -> None:
        diff = "+++ b/main.py\n+def new_func():\n+    pass"
        ctx = make_pr_context()
        check = CoverageCheck()
        _system, user = check.build_prompt(diff, ctx)
        assert "new_func" in user
        assert "```diff" in user

    def test_user_message_includes_pr_metadata(self) -> None:
        ctx = make_pr_context(
            pr_number=99,
            pr_title="Add widget feature",
            pr_author="bob",
            project_name="acme/widgets",
        )
        check = CoverageCheck()
        _system, user = check.build_prompt("diff", ctx)
        assert "PR #99" in user
        assert "Add widget feature" in user
        assert "bob" in user
        assert "acme/widgets" in user

    def test_user_message_includes_pr_body(self) -> None:
        ctx = make_pr_context(pr_body="This adds a caching layer.")
        check = CoverageCheck()
        _system, user = check.build_prompt("diff", ctx)
        assert "caching layer" in user

    def test_user_message_truncates_long_body(self) -> None:
        long_body = "A" * 3000
        ctx = make_pr_context(pr_body=long_body)
        check = CoverageCheck()
        _system, user = check.build_prompt("diff", ctx)
        assert "(truncated)" in user
        # First 2000 chars present, full 3000 not
        assert "A" * 2000 in user

    def test_user_message_handles_empty_body(self) -> None:
        ctx = make_pr_context(pr_body="")
        check = CoverageCheck()
        _system, user = check.build_prompt("diff", ctx)
        assert "PR #" in user  # still has header


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
