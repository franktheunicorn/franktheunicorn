"""Tests for the issue-link sub-check."""

from __future__ import annotations

import json

from franktheunicorn.review.checks.issue_link import IssueLinkCheck
from tests.conftest import make_pr_context


class TestIssueLinkCheckPrompt:
    def test_system_prompt_includes_schema(self) -> None:
        ctx = make_pr_context()
        check = IssueLinkCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "issue-link" in system
        assert "findings" in system
        assert "file_path" in system  # schema fields present

    def test_system_prompt_includes_github_issue_context(self) -> None:
        ctx = make_pr_context(
            linked_issues_context="GitHub Issue #99: Fix login bug\nState: open | Author: bob"
        )
        check = IssueLinkCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "Fix login bug" in system
        assert "Linked GitHub issue(s)" in system

    def test_system_prompt_includes_jira_context(self) -> None:
        ctx = make_pr_context(jira_context="SPARK-12345: Improve shuffle performance")
        check = IssueLinkCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "SPARK-12345" in system
        assert "Linked JIRA ticket" in system

    def test_system_prompt_includes_both_github_and_jira(self) -> None:
        ctx = make_pr_context(
            linked_issues_context="GitHub Issue #10: Add caching",
            jira_context="SPARK-100: Cache layer",
        )
        check = IssueLinkCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "Linked GitHub issue(s)" in system
        assert "Linked JIRA ticket" in system

    def test_system_prompt_handles_no_linked_issues(self) -> None:
        ctx = make_pr_context(linked_issues_context="", jira_context="")
        check = IssueLinkCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "No linked issues were found" in system
        assert "empty findings array" in system

    def test_user_message_delegates_to_build_user_message(self) -> None:
        diff = "+++ b/main.py\n+def new_func():\n+    pass"
        ctx = make_pr_context(pr_number=99, pr_title="Add widget")
        check = IssueLinkCheck()
        _system, user = check.build_prompt(diff, ctx)
        assert "PR #99" in user
        assert "Add widget" in user
        assert "new_func" in user
        assert "```diff" in user


class TestIssueLinkCheckName:
    def test_name_is_issue_link(self) -> None:
        assert IssueLinkCheck.name == "issue-link"
        assert IssueLinkCheck().name == "issue-link"


class TestIssueLinkCheckParsing:
    def test_parse_valid_findings(self) -> None:
        check = IssueLinkCheck()
        raw = json.dumps(
            {
                "findings": [
                    {
                        "file_path": "",
                        "title": "issue-link: PR does not address linked issue",
                        "body": "Issue #42 is about login bugs but this PR changes the dashboard.",
                        "confidence": 0.8,
                        "severity": "important",
                    }
                ]
            }
        )
        findings = check.parse_response(raw)
        assert len(findings) == 1
        assert findings[0].severity == "important"
        assert "login bugs" in findings[0].body

    def test_parse_empty_findings(self) -> None:
        check = IssueLinkCheck()
        findings = check.parse_response('{"findings": []}')
        assert findings == []

    def test_parse_invalid_json(self) -> None:
        check = IssueLinkCheck()
        findings = check.parse_response("not json at all")
        assert findings == []

    def test_parse_markdown_wrapped(self) -> None:
        check = IssueLinkCheck()
        raw = '```json\n{"findings": [{"body": "wrong issue", "severity": "nit"}]}\n```'
        findings = check.parse_response(raw)
        assert len(findings) == 1
        assert findings[0].body == "wrong issue"
