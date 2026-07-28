"""Tests for the claude-code LLM backend (shells out to the local claude CLI).

These tests never invoke the real ``claude`` binary — the executor
returned by ``make_executor`` is monkeypatched to a fake whose ``run``
returns a canned ``ExecResult``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig
from franktheunicorn.review.backends.claude_code_backend import ClaudeCodeBackend
from franktheunicorn.review.tool_executor import ExecResult
from tests.conftest import make_pr_context
from tests.factories import ProjectFactory

_MAKE_EXECUTOR = "franktheunicorn.review.backends.claude_code_backend.make_executor"

_SAMPLE_DIFF = """\
diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,4 @@
 import os
+import sys
"""


def _fake_executor(result: ExecResult | None) -> MagicMock:
    executor = MagicMock()
    executor.run.return_value = result
    return executor


def _cli_json(
    result_text: str, *, is_error: bool = False, tokens_in: int = 0, tokens_out: int = 0
) -> str:
    return json.dumps(
        {
            "result": result_text,
            "is_error": is_error,
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
            "total_cost_usd": 0.0,
        }
    )


class TestClaudeCodeBackendAvailability:
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_binary_found_marks_available(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        assert backend._sdk_available is True

    @patch("shutil.which", return_value=None)
    def test_missing_binary_marks_unavailable(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        assert backend._sdk_available is False

    @patch("shutil.which", return_value=None)
    def test_missing_binary_generate_review_returns_empty(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        result = backend.generate_review(_SAMPLE_DIFF, make_pr_context())
        assert result.findings == []
        assert result.overall_vibe == ""

    @patch("shutil.which", return_value=None)
    def test_missing_binary_complete_returns_empty_string(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        assert backend.complete("hello") == ""


class TestClaudeCodeBackendComplete:
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_complete_returns_parsed_result_text(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        cli_stdout = _cli_json("here is my answer", tokens_in=12, tokens_out=34)
        executor = _fake_executor(ExecResult(returncode=0, stdout=cli_stdout, stderr=""))

        with patch(_MAKE_EXECUTOR, return_value=executor):
            text = backend.complete("what's up?", system="be terse")

        assert text == "here is my answer"
        assert backend._last_tokens_in == 12
        assert backend._last_tokens_out == 34

    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_argv_includes_prompt_model_and_disallowed_tools(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code", model="opus"))
        executor = _fake_executor(ExecResult(returncode=0, stdout=_cli_json("ok"), stderr=""))

        with patch(_MAKE_EXECUTOR, return_value=executor):
            backend.complete("do a thing", system="sys prompt")

        argv = executor.run.call_args.args[0]
        assert argv[0] == "claude"
        assert "-p" in argv
        assert argv[argv.index("-p") + 1] == "do a thing"
        assert "--output-format" in argv
        assert "--system-prompt" in argv
        assert argv[argv.index("--system-prompt") + 1] == "sys prompt"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "opus"
        assert "--disallowedTools" in argv

    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_non_json_stdout_falls_back_to_plain_text(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        executor = _fake_executor(ExecResult(returncode=0, stdout="plain text reply", stderr=""))

        with patch(_MAKE_EXECUTOR, return_value=executor):
            text = backend.complete("hi")

        assert text == "plain text reply"

    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_is_error_flag_raises_and_complete_swallows_it(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        cli_stdout = _cli_json("something broke", is_error=True)
        executor = _fake_executor(ExecResult(returncode=0, stdout=cli_stdout, stderr=""))

        with patch(_MAKE_EXECUTOR, return_value=executor):
            text = backend.complete("hi")

        assert text == ""


class TestClaudeCodeBackendGenerateReview:
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_generate_review_parses_findings_json(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        review_json = json.dumps(
            {
                "overall_vibe": "Looks reasonable.",
                "findings": [
                    {
                        "file_path": "src/main.py",
                        "line_number": 2,
                        "title": "Unused import",
                        "body": "sys is imported but never used.",
                        "severity": "low",
                    }
                ],
            }
        )
        cli_stdout = _cli_json(review_json, tokens_in=100, tokens_out=50)
        executor = _fake_executor(ExecResult(returncode=0, stdout=cli_stdout, stderr=""))

        with patch(_MAKE_EXECUTOR, return_value=executor):
            result = backend.generate_review(_SAMPLE_DIFF, make_pr_context())

        assert result.overall_vibe == "Looks reasonable."
        assert len(result.findings) == 1
        assert result.findings[0].file_path == "src/main.py"
        assert result.findings[0].confidence == 0.4  # low severity -> 0.4


class TestClaudeCodeBackendFailureHandling:
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_executor_returns_none_handled_by_generate_review(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        executor = _fake_executor(None)

        with patch(_MAKE_EXECUTOR, return_value=executor):
            result = backend.generate_review(_SAMPLE_DIFF, make_pr_context())

        assert result.findings == []

    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_nonzero_exit_handled_by_complete(self, _mock_which: MagicMock) -> None:
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        executor = _fake_executor(ExecResult(returncode=1, stdout="", stderr="rate limited"))

        with patch(_MAKE_EXECUTOR, return_value=executor):
            text = backend.complete("hi")

        assert text == ""


@pytest.mark.django_db
class TestClaudeCodeBackendCostRecording:
    @patch("shutil.which", return_value="/usr/local/bin/claude")
    def test_record_cost_produces_zero_dollar_record(self, _mock_which: MagicMock) -> None:
        from franktheunicorn.core.models import CostRecord

        project = ProjectFactory()
        backend = ClaudeCodeBackend(LLMBackendConfig(provider="claude-code"))
        cli_stdout = _cli_json("ok", tokens_in=200, tokens_out=80)
        executor = _fake_executor(ExecResult(returncode=0, stdout=cli_stdout, stderr=""))

        with patch(_MAKE_EXECUTOR, return_value=executor):
            backend.metered_call(
                "sys",
                "user",
                action_type="triage",
                project_id=project.id,
            )

        record = CostRecord.objects.get(project=project)
        assert record.tokens_in == 200
        assert record.tokens_out == 80
        assert record.estimated_cost_usd == Decimal("0.000000")
