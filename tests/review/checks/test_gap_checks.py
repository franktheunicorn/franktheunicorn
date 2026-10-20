"""Tests for the gap sub-checks: correctness, concurrency, edge-case,
doc-accuracy, design (see docs/review-categories.md)."""

from __future__ import annotations

import pytest

from franktheunicorn.review.checks import _get_registry
from franktheunicorn.review.checks.concurrency import ConcurrencyCheck
from franktheunicorn.review.checks.correctness import CorrectnessCheck
from franktheunicorn.review.checks.design import DesignCheck
from franktheunicorn.review.checks.doc_accuracy import DocAccuracyCheck
from franktheunicorn.review.checks.edge_case import EdgeCaseCheck
from franktheunicorn.review.drafter import _derive_category
from tests.conftest import make_pr_context

# (check class, registry key / name, category word the prompt pins)
GAP_CHECKS = [
    (CorrectnessCheck, "correctness", "correctness"),
    (ConcurrencyCheck, "concurrency", "concurrency"),
    (EdgeCaseCheck, "edge-case", "edge-case"),
    (DocAccuracyCheck, "doc-accuracy", "doc-accuracy"),
    (DesignCheck, "design", "design"),
]


class TestGapCheckRegistration:
    @pytest.mark.parametrize(("cls", "key", "_cat"), GAP_CHECKS)
    def test_registered_under_its_name(self, cls: type, key: str, _cat: str) -> None:
        registry = _get_registry()
        assert key in registry
        assert registry[key] is cls
        assert cls.name == key


class TestGapCheckPrompts:
    @pytest.mark.parametrize(("cls", "_key", "cat"), GAP_CHECKS)
    def test_prompt_pins_category_and_schema(self, cls: type, _key: str, cat: str) -> None:
        ctx = make_pr_context()
        system, _user = cls().build_prompt("+ some diff", ctx)
        # Category word is in the system prompt (so findings carry it) ...
        assert cat in system
        # ... and the JSON schema is present so the model returns findings.
        assert "findings" in system
        assert "file_path" in system
        assert "empty findings array" in system

    def test_user_message_contains_diff_and_pr(self) -> None:
        ctx = make_pr_context(pr_number=42, pr_title="Fix the thing")
        _system, user = CorrectnessCheck().build_prompt("+++ b/x.scala\n+def f(): Int = 1", ctx)
        assert "PR #42" in user
        assert "Fix the thing" in user
        assert "```diff" in user

    def test_design_injects_project_guidance(self) -> None:
        ctx = make_pr_context(
            review_guidance="Spark SQL: watch the Catalyst analyzer",
            review_areas_of_interest=["AQE", "shuffle"],
        )
        system, _user = DesignCheck().build_prompt("+ diff", ctx)
        assert "Spark SQL: watch the Catalyst analyzer" in system
        assert "AQE" in system

    def test_design_without_guidance_still_builds(self) -> None:
        ctx = make_pr_context(review_guidance="", review_areas_of_interest=[])
        system, _user = DesignCheck().build_prompt("+ diff", ctx)
        assert "design" in system


class TestGapCategoryDerivation:
    @pytest.mark.parametrize(("_cls", "_key", "cat"), GAP_CHECKS)
    def test_category_word_maps_back(self, _cls: type, _key: str, cat: str) -> None:
        # A finding whose title carries the category word resolves to that
        # category in the draft pipeline.
        assert _derive_category(f"{cat}: something is off at line 5") == cat

    def test_doc_accuracy_not_shadowed_by_design(self) -> None:
        # A doc-accuracy finding quoting "the design doc" must still resolve
        # to doc-accuracy, not design (ordering in _derive_category).
        assert _derive_category("doc-accuracy: the design doc says X") == "doc-accuracy"
