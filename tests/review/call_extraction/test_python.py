"""Tests for the Python call-site extractor."""

from __future__ import annotations

from franktheunicorn.review.call_extraction import extract_calls
from franktheunicorn.review.call_extraction.types import Language


def _diff(body: str) -> str:
    return body


class TestPythonExtraction:
    def test_resolves_module_alias(self) -> None:
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,4 @@
+import pandas as pd
+
+def f(df):
+    return pd.DataFrame.apply(df, lambda x: x)
"""
        sites = extract_calls(diff)
        assert len(sites) == 1
        site = sites[0]
        assert site.language is Language.PYTHON
        assert site.package == "pandas"
        assert site.qualified_name == "pandas.DataFrame.apply"
        assert site.file_path == "foo.py"
        assert site.line_number == 4

    def test_resolves_from_import(self) -> None:
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,3 @@
+from requests import get
+
+resp = get("https://example.com")
"""
        sites = extract_calls(diff)
        assert len(sites) == 1
        site = sites[0]
        assert site.package == "requests"
        assert site.qualified_name == "requests.get"

    def test_skips_stdlib(self) -> None:
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,3 @@
+import os
+
+os.path.join("a", "b")
"""
        sites = extract_calls(diff)
        assert sites == []

    def test_skips_first_party(self) -> None:
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,3 @@
+from myproj.utils import helper
+
+helper(42)
"""
        sites = extract_calls(diff, project_packages=["myproj"])
        assert sites == []

    def test_ignores_non_python_files(self) -> None:
        diff = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -0,0 +1,2 @@
+import pandas as pd
+pd.DataFrame.apply(df)
"""
        sites = extract_calls(diff)
        assert sites == []

    def test_dedupes_identical_call_on_same_line(self) -> None:
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,3 @@
+import pandas as pd
+
+x = pd.DataFrame()
"""
        sites = extract_calls(diff)
        # Single call (the AST path resolves it once; regex fallback does not run).
        assert len(sites) == 1
        assert sites[0].qualified_name == "pandas.DataFrame"

    def test_handles_unparseable_lines_via_regex_fallback(self) -> None:
        # An indented continuation line that doesn't parse standalone.
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,3 @@
+import pandas as pd
+
+    pd.DataFrame.apply(
"""
        sites = extract_calls(diff)
        assert len(sites) == 1
        assert sites[0].qualified_name.startswith("pandas")

    def test_alias_with_asname(self) -> None:
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,3 @@
+from numpy import array as arr
+
+arr([1, 2, 3])
"""
        sites = extract_calls(diff)
        assert len(sites) == 1
        assert sites[0].package == "numpy"
        assert sites[0].qualified_name == "numpy.array"

    def test_empty_diff(self) -> None:
        assert extract_calls("") == []

    def test_invalid_diff_returns_empty(self) -> None:
        # Header that fails to parse as a unified diff.
        assert extract_calls("not a diff at all") == []
