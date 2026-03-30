"""Tests for prompt construction."""

from __future__ import annotations

from franktheunicorn.review.prompt import build_system_prompt, build_user_message
from tests.conftest import make_pr_context


class TestBuildSystemPrompt:
    def test_includes_review_style(self) -> None:
        ctx = make_pr_context(review_style="thorough and formal")
        prompt = build_system_prompt(ctx)
        assert "thorough and formal" in prompt

    def test_includes_tone(self) -> None:
        ctx = make_pr_context(tone="friendly")
        prompt = build_system_prompt(ctx)
        assert "friendly" in prompt

    def test_includes_governance(self) -> None:
        ctx = make_pr_context(governance="asf")
        prompt = build_system_prompt(ctx)
        assert "asf" in prompt

    def test_includes_anti_patterns(self) -> None:
        ctx = make_pr_context(anti_patterns=["nit: fix spacing", "consider adding a test"])
        prompt = build_system_prompt(ctx)
        assert "nit: fix spacing" in prompt
        assert "consider adding a test" in prompt
        assert "anti-pattern" in prompt.lower()

    def test_includes_json_schema(self) -> None:
        ctx = make_pr_context()
        prompt = build_system_prompt(ctx)
        assert "file_path" in prompt
        assert "severity" in prompt
        assert "findings" in prompt

    def test_skips_default_governance(self) -> None:
        ctx = make_pr_context(governance="standard")
        prompt = build_system_prompt(ctx)
        assert "Governance" not in prompt

    def test_skips_default_review_context(self) -> None:
        ctx = make_pr_context(review_context="general open-source")
        prompt = build_system_prompt(ctx)
        assert "Project context" not in prompt


class TestBuildUserMessage:
    def test_includes_pr_metadata(self) -> None:
        ctx = make_pr_context()
        msg = build_user_message("diff content", ctx)
        assert "PR #42" in msg
        assert "Fix flaky test" in msg
        assert "alice" in msg
        assert "apache/spark" in msg

    def test_includes_diff(self) -> None:
        ctx = make_pr_context()
        diff = "+++ b/src/main.py\n+import sys\n"
        msg = build_user_message(diff, ctx)
        assert "+import sys" in msg

    def test_includes_pr_body(self) -> None:
        ctx = make_pr_context(pr_body="Fixes race condition in scheduler.")
        msg = build_user_message("diff", ctx)
        assert "race condition" in msg

    def test_truncates_long_body(self) -> None:
        ctx = make_pr_context(pr_body="x" * 3000)
        msg = build_user_message("diff", ctx)
        assert "truncated" in msg


class TestFindingSchemaGeneration:
    """Verify the schema is auto-generated from the ReviewFinding Pydantic model."""

    def test_schema_contains_all_model_fields(self) -> None:
        from franktheunicorn.review.backends.base import ReviewFinding
        from franktheunicorn.review.prompt import _finding_schema

        schema = _finding_schema()
        for field_name in ReviewFinding.model_fields:
            assert field_name in schema, f"Missing field '{field_name}' in generated schema"

    def test_schema_is_cached(self) -> None:
        from franktheunicorn.review.prompt import _finding_schema

        assert _finding_schema() is _finding_schema()  # same object = lru_cache hit
