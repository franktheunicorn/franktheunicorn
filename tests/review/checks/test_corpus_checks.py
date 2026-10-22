"""Tests for the corpus sub-checks: test-pinning, parity, config-docs,
error-framework (see docs/review-categories.md, HyukjinKwon corpus audit)."""

from __future__ import annotations

import pytest

from franktheunicorn.review.checks import _get_registry
from franktheunicorn.review.checks.config_docs import ConfigDocsCheck
from franktheunicorn.review.checks.error_framework import ErrorFrameworkCheck
from franktheunicorn.review.checks.parity import ParityCheck
from franktheunicorn.review.checks.test_pinning import TestPinningCheck
from franktheunicorn.review.drafter import _derive_category
from tests.conftest import make_pr_context

# (check class, registry key / name, category word the prompt pins)
CORPUS_CHECKS = [
    (TestPinningCheck, "test-pinning", "test-pinning"),
    (ParityCheck, "parity", "parity"),
    (ConfigDocsCheck, "config-docs", "config-docs"),
    (ErrorFrameworkCheck, "error-framework", "error-framework"),
]


class TestCorpusCheckRegistration:
    @pytest.mark.parametrize(("cls", "key", "_cat"), CORPUS_CHECKS)
    def test_registered_under_its_name(self, cls: type, key: str, _cat: str) -> None:
        registry = _get_registry()
        assert key in registry
        assert registry[key] is cls
        assert cls.name == key


class TestCorpusCheckPrompts:
    @pytest.mark.parametrize(("cls", "_key", "cat"), CORPUS_CHECKS)
    def test_prompt_pins_category_and_schema(self, cls: type, _key: str, cat: str) -> None:
        ctx = make_pr_context()
        system, _user = cls().build_prompt("+ some diff", ctx)
        assert cat in system
        assert "findings" in system
        assert "file_path" in system
        assert "empty findings array" in system

    @pytest.mark.parametrize(("cls", "_key", "_cat"), CORPUS_CHECKS)
    def test_user_message_contains_diff_and_pr(self, cls: type, _key: str, _cat: str) -> None:
        ctx = make_pr_context(pr_number=42, pr_title="Fix the thing")
        _system, user = cls().build_prompt("+++ b/x.py\n+def f(): return 1", ctx)
        assert "PR #42" in user
        assert "Fix the thing" in user
        assert "```diff" in user

    def test_test_pinning_injects_test_expectations(self) -> None:
        ctx = make_pr_context(test_expectations="tests required for bug fixes")
        system, _user = TestPinningCheck().build_prompt("+ diff", ctx)
        assert "tests required for bug fixes" in system

    def test_test_pinning_without_expectations_still_builds(self) -> None:
        ctx = make_pr_context(test_expectations="")
        system, _user = TestPinningCheck().build_prompt("+ diff", ctx)
        assert "test-pinning" in system

    def test_config_docs_injects_project_guidance(self) -> None:
        ctx = make_pr_context(
            review_guidance="Versioning: since-version names the branch's next release"
        )
        system, _user = ConfigDocsCheck().build_prompt("+ diff", ctx)
        assert "since-version names the branch's next release" in system

    def test_config_docs_without_guidance_still_builds(self) -> None:
        ctx = make_pr_context(review_guidance="")
        system, _user = ConfigDocsCheck().build_prompt("+ diff", ctx)
        assert "config-docs" in system

    def test_error_framework_injects_project_guidance(self) -> None:
        ctx = make_pr_context(review_guidance="Register error conditions in error-conditions.json")
        system, _user = ErrorFrameworkCheck().build_prompt("+ diff", ctx)
        assert "error-conditions.json" in system

    def test_parity_builds_without_extras(self) -> None:
        ctx = make_pr_context(review_guidance="", review_areas_of_interest=[])
        system, _user = ParityCheck().build_prompt("+ diff", ctx)
        assert "parity" in system


class TestCorpusCategoryDerivation:
    @pytest.mark.parametrize(("_cls", "_key", "cat"), CORPUS_CHECKS)
    def test_category_word_maps_back(self, _cls: type, _key: str, cat: str) -> None:
        assert _derive_category(f"{cat}: something is off at line 5") == cat

    def test_test_pinning_not_shadowed_by_test_coverage(self) -> None:
        # "test-pinning" must not fall into "test-coverage" (ordering in
        # _derive_category): the two checks own different asks.
        assert _derive_category("test-pinning: suite stays green if reverted") == "test-pinning"
