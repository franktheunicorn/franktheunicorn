"""Tests for the Java call-site extractor."""

from __future__ import annotations

from franktheunicorn.review.call_extraction import extract_calls
from franktheunicorn.review.call_extraction.types import Language


class TestJavaExtraction:
    def test_resolves_imported_class_call(self) -> None:
        diff = """\
diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -0,0 +1,7 @@
+package com.example;
+import com.google.common.collect.ImmutableList;
+
+public class Foo {
+    void m() {
+        ImmutableList.copyOf(new int[]{1,2,3});
+    }
"""
        sites = extract_calls(diff)
        assert len(sites) == 1
        site = sites[0]
        assert site.language is Language.JAVA
        assert site.qualified_name == "com.google.common.collect.ImmutableList.copyOf"
        assert site.package == "com.google.common"
        assert site.file_path == "Foo.java"
        assert site.line_number == 6

    def test_skips_stdlib_imports(self) -> None:
        diff = """\
diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -0,0 +1,5 @@
+import java.util.ArrayList;
+
+public class Foo {
+    ArrayList.class.getName();
+}
"""
        sites = extract_calls(diff)
        assert sites == []

    def test_skips_first_party(self) -> None:
        diff = """\
diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -0,0 +1,4 @@
+import com.example.helpers.Helper;
+
+public class Foo {
+    Helper.run(); }
"""
        sites = extract_calls(diff, project_package="com.example")
        assert sites == []

    def test_ignores_wildcard_imports(self) -> None:
        diff = """\
diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -0,0 +1,4 @@
+import com.google.common.collect.*;
+
+public class Foo {
+    ImmutableList.copyOf(x); }
"""
        sites = extract_calls(diff)
        # Wildcard imports are skipped — no FQCN binding for ImmutableList.
        assert sites == []

    def test_static_import(self) -> None:
        diff = """\
diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -0,0 +1,4 @@
+import static com.google.common.base.Preconditions.checkNotNull;
+
+public class Foo {
+    Object x; }
"""
        # Static imports bind a method symbol, not a class — extractor's
        # current matcher requires Identifier.method(. We don't surface them.
        sites = extract_calls(diff)
        assert sites == []

    def test_ignores_non_java_files(self) -> None:
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1,2 @@
+import com.example.X;
+X.method();
"""
        # No .java files in the diff → nothing for the Java extractor.
        sites = extract_calls(diff)
        assert all(s.language is not Language.JAVA for s in sites)
