"""Tests for the api-misuse review sub-check."""

from __future__ import annotations

from franktheunicorn.config.models import APIMisuseConfig
from franktheunicorn.data_access.package_registry import PackageDocs, Registry
from franktheunicorn.review.checks.api_misuse import APIMisuseCheck, format_docs_block
from tests.conftest import make_pr_context

_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,4 @@
+import pandas as pd
+
+def f(df):
+    return pd.DataFrame.apply(df, lambda x: x)
"""


class TestAPIMisuseCheckPrompt:
    def test_disabled_config_skips_network_calls(self) -> None:
        ctx = make_pr_context()
        check = APIMisuseCheck(config=APIMisuseConfig(enabled=False))
        system, user = check.build_prompt(_DIFF, ctx)
        assert "api-misuse" in system.lower() or "API-misuse" in system
        # No docs block when feature is disabled, even if calls were extracted.
        assert "Upstream docs" not in user

    def test_no_calls_no_docs_block(self) -> None:
        ctx = make_pr_context()
        check = APIMisuseCheck(config=APIMisuseConfig(enabled=True))
        # Stdlib only — extractor returns nothing, so no docs block.
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,2 @@
+import os
+os.path.join("a", "b")
"""
        _system, user = check.build_prompt(diff, ctx)
        assert "Upstream docs" not in user
        assert "PR #" in user  # base prompt still rendered

    def test_check_name(self) -> None:
        assert APIMisuseCheck.name == "api-misuse"
        assert APIMisuseCheck().name == "api-misuse"

    def test_system_prompt_lists_misuse_categories(self) -> None:
        ctx = make_pr_context()
        check = APIMisuseCheck(config=APIMisuseConfig(enabled=False))
        system, _user = check.build_prompt("", ctx)
        assert "deprecated" in system.lower()
        assert "complexity" in system.lower() or "O(N" in system

    def test_findings_must_be_prefixed(self) -> None:
        ctx = make_pr_context()
        check = APIMisuseCheck(config=APIMisuseConfig(enabled=False))
        system, _user = check.build_prompt("", ctx)
        assert "api-misuse:" in system


class TestFormatDocsBlock:
    def test_renders_signature_and_complexity(self) -> None:
        d = PackageDocs(
            registry=Registry.PYPI,
            package="pandas",
            version="2.0.0",
            qualified_name="pandas.DataFrame.apply",
            signature="DataFrame.apply(func, axis=0)",
            docstring="Apply a function.",
            complexity_notes="O(N*M)",
            doc_url="https://pandas/docs/apply.html",
        )
        block = format_docs_block([d])
        assert "pandas.DataFrame.apply" in block
        assert "DataFrame.apply(func, axis=0)" in block
        assert "O(N*M)" in block
        assert "https://pandas/docs/apply.html" in block

    def test_marks_deprecated(self) -> None:
        d = PackageDocs(
            registry=Registry.PYPI,
            package="pandas",
            qualified_name="pandas.DataFrame.ix",
            deprecated=True,
            deprecation_message="Deprecated since 0.20.0; use .loc",
        )
        block = format_docs_block([d])
        assert "deprecated" in block.lower()
        assert "0.20.0" in block

    def test_empty_input(self) -> None:
        block = format_docs_block([])
        assert "no upstream docs" in block.lower()
