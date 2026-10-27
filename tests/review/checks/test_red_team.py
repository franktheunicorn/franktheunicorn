"""Tests for the red-team sub-check."""

from __future__ import annotations

import json

from franktheunicorn.review.checks.red_team import RedTeamCheck
from tests.conftest import make_pr_context


class TestRedTeamCheckPrompt:
    def test_system_prompt_includes_schema(self) -> None:
        ctx = make_pr_context()
        check = RedTeamCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "findings" in system
        assert "file_path" in system  # schema fields present

    def test_system_prompt_uses_red_team_framing(self) -> None:
        ctx = make_pr_context()
        check = RedTeamCheck()
        system, _user = check.build_prompt("diff here", ctx)
        # Offensive framing, not the defensive "does this introduce a vuln".
        assert "red-team" in system.lower()
        assert "attacker" in system.lower()
        assert "exploit" in system.lower()

    def test_system_prompt_reads_prior_comments(self) -> None:
        ctx = make_pr_context()
        check = RedTeamCheck()
        system, _user = check.build_prompt("diff here", ctx)
        # The check must lean on what other reviewers said.
        assert "existing PR comments" in system
        assert "reviewer" in system.lower()

    def test_system_prompt_forbids_working_exploit(self) -> None:
        ctx = make_pr_context()
        check = RedTeamCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "Do NOT write a working exploit" in system
        assert "proof-of-concept" in system

    def test_system_prompt_requires_named_route(self) -> None:
        ctx = make_pr_context()
        check = RedTeamCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "trust boundary" in system.lower()
        assert "route" in system.lower()

    def test_security_model_is_included_when_set(self) -> None:
        ctx = make_pr_context(security_model="Submitted jobs run arbitrary code by design.")
        check = RedTeamCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "Submitted jobs run arbitrary code by design." in system
        assert "NOT a finding" in system

    def test_security_model_section_omitted_when_empty(self) -> None:
        ctx = make_pr_context(security_model="")
        check = RedTeamCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "Project security model" not in system

    def test_system_prompt_excludes_non_security_concerns(self) -> None:
        ctx = make_pr_context()
        check = RedTeamCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "Do NOT comment on code style" in system

    def test_title_prefix_is_red_team(self) -> None:
        ctx = make_pr_context()
        check = RedTeamCheck()
        system, _user = check.build_prompt("diff here", ctx)
        assert "red-team:" in system


class TestRedTeamCheckUserMessage:
    def test_user_message_delegates_to_build_user_message(self) -> None:
        """User message is built by the shared helper, so prior comments + diff
        reach the check the same way they reach every other check."""
        diff = "+++ b/auth.py\n+def login(user): return grant(user)\n"
        ctx = make_pr_context(pr_number=99, pr_title="Add login")
        check = RedTeamCheck()
        _system, user = check.build_prompt(diff, ctx)
        assert "PR #99" in user
        assert "Add login" in user
        assert "grant" in user
        assert "```diff" in user

    def test_user_message_includes_prior_comments_when_present(self) -> None:
        from franktheunicorn.review.prior_comments import (
            PriorComment,
            format_prior_comments,
        )

        diff = "+pass"
        # Match the real flow: run_enabled_checks formats prior comments into
        # the pr_context string before the check ever sees it.
        formatted = format_prior_comments(
            [PriorComment(author="bob", body="the error path leaks the user id")]
        )
        ctx = make_pr_context(prior_comments=formatted)
        check = RedTeamCheck()
        _system, user = check.build_prompt(diff, ctx)
        assert "EXISTING PR COMMENTS" in user
        assert "the error path leaks the user id" in user


class TestRedTeamCheckName:
    def test_name_is_red_team(self) -> None:
        assert RedTeamCheck.name == "red-team"
        assert RedTeamCheck().name == "red-team"


class TestRedTeamCheckParsing:
    def test_parse_valid_findings(self) -> None:
        check = RedTeamCheck()
        raw = json.dumps(
            {
                "findings": [
                    {
                        "file_path": "src/api.py",
                        "line_number": 88,
                        "title": "red-team: new endpoint skips the auth check",
                        "body": (
                            "The new /internal/user route has no auth check; an "
                            "unauthenticated caller can enumerate user ids."
                        ),
                        "confidence": 0.85,
                        "severity": "critical",
                    }
                ]
            }
        )
        findings = check.parse_response(raw)
        assert len(findings) == 1
        assert findings[0].file_path == "src/api.py"
        assert findings[0].severity == "critical"
        assert findings[0].title.startswith("red-team:")

    def test_parse_empty_findings(self) -> None:
        check = RedTeamCheck()
        findings = check.parse_response('{"findings": []}')
        assert findings == []

    def test_parse_invalid_json(self) -> None:
        check = RedTeamCheck()
        findings = check.parse_response("not json at all")
        assert findings == []

    def test_parse_markdown_wrapped(self) -> None:
        check = RedTeamCheck()
        raw = '```json\n{"findings": [{"body": "path traverses up", "severity": "important"}]}\n```'
        findings = check.parse_response(raw)
        assert len(findings) == 1
        assert findings[0].body == "path traverses up"
