"""Tests for the pr-description sub-check."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.review.checks.pr_description import (
    PRDescriptionCheck,
    _build_user_message,
    _strip_html_comments,
)


class TestStripHtmlComments:
    def test_removes_single_comment(self) -> None:
        result = _strip_html_comments("## Summary\n<!-- describe here -->\n## Changes")
        assert "describe here" not in result
        assert "## Summary" in result

    def test_removes_multiline_comment(self) -> None:
        text = "Before\n<!-- line1\nline2\nline3 -->\nAfter"
        result = _strip_html_comments(text)
        assert "line1" not in result
        assert "Before" in result
        assert "After" in result

    def test_no_comments_unchanged(self) -> None:
        text = "## Summary\n\nFixed the thing."
        assert _strip_html_comments(text) == text

    def test_all_comments_returns_empty(self) -> None:
        text = "<!-- everything is a comment -->"
        assert _strip_html_comments(text) == ""


class TestBuildUserMessage:
    def test_includes_template_and_body(self) -> None:
        msg = _build_user_message("## Summary\n", "Fixed the bug.")
        parsed = json.loads(msg)
        assert parsed["pr_template"] == "## Summary\n"
        assert parsed["pr_description"] == "Fixed the bug."

    def test_empty_body_becomes_placeholder(self) -> None:
        msg = _build_user_message("## Summary\n", "")
        parsed = json.loads(msg)
        assert parsed["pr_description"] == "(empty)"

    def test_whitespace_only_body_becomes_placeholder(self) -> None:
        msg = _build_user_message("## Summary\n", "   \n  ")
        parsed = json.loads(msg)
        assert parsed["pr_description"] == "(empty)"


class TestPRDescriptionCheckName:
    def test_name(self) -> None:
        assert PRDescriptionCheck.name == "pr-description"
        assert PRDescriptionCheck().name == "pr-description"


class TestPRDescriptionCheckBuildPrompt:
    def test_build_prompt_includes_schema(self) -> None:
        from tests.conftest import make_pr_context

        check = PRDescriptionCheck()
        system, _user = check.build_prompt("diff", make_pr_context())
        assert "findings" in system
        assert "file_path" in system
        assert "HTML comments" in system


@pytest.mark.django_db
class TestPRDescriptionCheckScan:
    def test_no_template_returns_empty(self, db: Any) -> None:
        from tests.factories import PullRequestFactory

        pr = PullRequestFactory(body="Fixed the thing.")
        check = PRDescriptionCheck()

        with patch(
            "franktheunicorn.review.checks.pr_description._fetch_template",
            return_value="",
        ):
            findings = check.scan(pr, "", backend_config=MagicMock())

        assert findings == []

    def test_all_html_comment_template_returns_empty(self, db: Any) -> None:
        from tests.factories import PullRequestFactory

        pr = PullRequestFactory(body="Hello world.")
        check = PRDescriptionCheck()

        with patch(
            "franktheunicorn.review.checks.pr_description._fetch_template",
            return_value="<!-- This is just instructions for the author -->",
        ):
            findings = check.scan(pr, "", backend_config=MagicMock())

        assert findings == []

    def test_non_llm_backend_returns_empty(self, db: Any) -> None:
        from franktheunicorn.config.models import LLMBackendConfig
        from tests.factories import PullRequestFactory

        pr = PullRequestFactory(body="Fixed the thing.")
        check = PRDescriptionCheck()
        stub_config = LLMBackendConfig(provider="stub")

        with patch(
            "franktheunicorn.review.checks.pr_description._fetch_template",
            return_value="## Summary\n\n## Changes\n",
        ):
            findings = check.scan(pr, "", backend_config=stub_config)

        assert findings == []

    def test_llm_backend_called_with_template_content(self, db: Any) -> None:
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.backends.base import BaseLLMBackend
        from tests.factories import PullRequestFactory

        pr = PullRequestFactory(body="I fixed something.")
        check = PRDescriptionCheck()

        mock_backend = MagicMock(spec=BaseLLMBackend)
        mock_backend._resolve_api_key.return_value = "fake-key"
        mock_backend.metered_call.return_value = '{"findings": []}'

        config = LLMBackendConfig(provider="claude")
        with (
            patch(
                "franktheunicorn.review.checks.pr_description._fetch_template",
                return_value="## Summary\n\n## Changes\n",
            ),
            patch(
                "franktheunicorn.review.backends.get_backend",
                return_value=mock_backend,
            ),
        ):
            findings = check.scan(pr, "", backend_config=config)

        assert findings == []
        mock_backend.metered_call.assert_called_once()
        call_args = mock_backend.metered_call.call_args
        user_message = call_args[0][1]
        parsed = json.loads(user_message)
        assert "## Summary" in parsed["pr_template"]

    def test_llm_finding_returned_for_unfilled_placeholder(self, db: Any) -> None:
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.backends.base import BaseLLMBackend
        from tests.factories import PullRequestFactory

        pr = PullRequestFactory(body="TODO: describe what this PR does")
        check = PRDescriptionCheck()

        finding_json = json.dumps(
            {
                "findings": [
                    {
                        "file_path": "",
                        "line_number": None,
                        "title": "pr-description: unfilled placeholder",
                        "body": "The PR description still contains placeholder text.",
                        "confidence": 0.85,
                        "severity": "important",
                    }
                ]
            }
        )

        mock_backend = MagicMock(spec=BaseLLMBackend)
        mock_backend._resolve_api_key.return_value = "fake-key"
        mock_backend.metered_call.return_value = finding_json

        config = LLMBackendConfig(provider="claude")
        with (
            patch(
                "franktheunicorn.review.checks.pr_description._fetch_template",
                return_value="## Summary\n\n## Changes\n",
            ),
            patch(
                "franktheunicorn.review.backends.get_backend",
                return_value=mock_backend,
            ),
        ):
            findings = check.scan(pr, "", backend_config=config)

        assert len(findings) == 1
        assert findings[0].severity == "important"
        assert "placeholder" in findings[0].body

    def test_llm_exception_returns_empty(self, db: Any) -> None:
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.backends.base import BaseLLMBackend
        from tests.factories import PullRequestFactory

        pr = PullRequestFactory(body="Some description.")
        check = PRDescriptionCheck()

        mock_backend = MagicMock(spec=BaseLLMBackend)
        mock_backend._resolve_api_key.return_value = "fake-key"
        mock_backend.metered_call.side_effect = RuntimeError("API down")

        config = LLMBackendConfig(provider="claude")
        with (
            patch(
                "franktheunicorn.review.checks.pr_description._fetch_template",
                return_value="## Summary\n",
            ),
            patch(
                "franktheunicorn.review.backends.get_backend",
                return_value=mock_backend,
            ),
        ):
            findings = check.scan(pr, "", backend_config=config)

        assert findings == []


class TestRegistry:
    def test_pr_description_in_registry(self) -> None:
        from franktheunicorn.review.checks import _get_registry

        registry = _get_registry()
        assert "pr-description" in registry
        assert registry["pr-description"] is PRDescriptionCheck


class TestConfigValidator:
    def test_pr_description_is_known_check(self, caplog: pytest.LogCaptureFixture) -> None:
        from franktheunicorn.config.models import ProjectConfig

        with caplog.at_level("WARNING"):
            ProjectConfig(owner="x", repo="y", llm_checks=["pr-description"])

        assert not any("Unknown llm_check" in r.message for r in caplog.records)
