"""Tests for the pom.xml / build.sbt resolver."""

from __future__ import annotations

from franktheunicorn.data_access.package_registry.build_files import (
    BuildFileDep,
    collect_deps_from_diff,
    match_package_to_dep,
    parse_build_sbt,
    parse_pom_xml,
)

_POM_XML = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <properties>
        <guava.version>33.0.0-jre</guava.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>com.google.guava</groupId>
            <artifactId>guava</artifactId>
            <version>${guava.version}</version>
        </dependency>
        <dependency>
            <groupId>org.apache.commons</groupId>
            <artifactId>commons-lang3</artifactId>
            <version>3.14.0</version>
        </dependency>
    </dependencies>
</project>
"""


_BUILD_SBT = """
name := "demo"
libraryDependencies ++= Seq(
  "com.google.guava" % "guava" % "33.0.0-jre",
  "org.apache.spark" %% "spark-core" % "3.5.0" % Test,
)
"""


class TestParsePomXml:
    def test_parses_dependencies_with_property_resolution(self) -> None:
        deps = parse_pom_xml(_POM_XML)
        assert BuildFileDep("com.google.guava", "guava", "33.0.0-jre") in deps
        assert BuildFileDep("org.apache.commons", "commons-lang3", "3.14.0") in deps

    def test_returns_empty_for_blank_input(self) -> None:
        assert parse_pom_xml("") == []
        assert parse_pom_xml("   \n\t") == []

    def test_returns_empty_for_invalid_xml(self) -> None:
        assert parse_pom_xml("<not valid") == []

    def test_skips_dependencies_missing_required_fields(self) -> None:
        pom = """<?xml version="1.0"?>
<project>
  <dependencies>
    <dependency><groupId>only.group</groupId></dependency>
  </dependencies>
</project>"""
        assert parse_pom_xml(pom) == []

    def test_nested_property_reference_left_literal(self) -> None:
        # Nested resolution (one ${a} pointing at another) is not supported.
        # The single-pass resolver leaves the inner reference unexpanded —
        # explicitly documented behaviour, not silently broken.
        pom = """<?xml version="1.0"?>
<project>
<properties>
<outer>${inner}</outer>
<inner>33.0.0</inner>
</properties>
<dependencies>
<dependency><groupId>g</groupId><artifactId>a</artifactId>
<version>${outer}</version></dependency>
</dependencies>
</project>"""
        deps = parse_pom_xml(pom)
        assert len(deps) == 1
        assert deps[0].version == "${inner}"


class TestParseBuildSbt:
    def test_parses_inline_dependencies(self) -> None:
        deps = parse_build_sbt(_BUILD_SBT)
        assert BuildFileDep("com.google.guava", "guava", "33.0.0-jre") in deps
        assert BuildFileDep("org.apache.spark", "spark-core", "3.5.0") in deps

    def test_returns_empty_for_no_matches(self) -> None:
        assert parse_build_sbt('name := "empty"\n') == []


class TestMatchPackageToDep:
    def test_longest_group_prefix_wins(self) -> None:
        deps = [
            BuildFileDep("com.google", "google-base", "1.0"),
            BuildFileDep("com.google.guava", "guava", "33.0.0-jre"),
            BuildFileDep("org.apache", "commons", "3.0"),
        ]
        match = match_package_to_dep("com.google.guava.collect", deps)
        assert match is not None
        assert match.artifact == "guava"

    def test_exact_group_match(self) -> None:
        deps = [BuildFileDep("com.example", "thing", "1.0")]
        match = match_package_to_dep("com.example", deps)
        assert match is not None
        assert match.artifact == "thing"

    def test_artifact_name_fallback(self) -> None:
        deps = [BuildFileDep("io.foo", "bar-baz", "1.0")]
        # No group prefix match, but the artifact normalises to "barbaz"
        # which matches a segment of the package.
        match = match_package_to_dep("com.something.barbaz", deps)
        assert match is not None
        assert match.artifact == "bar-baz"

    def test_returns_none_when_nothing_matches(self) -> None:
        deps = [BuildFileDep("io.foo", "bar", "1.0")]
        assert match_package_to_dep("com.unrelated.pkg", deps) is None

    def test_handles_empty_inputs(self) -> None:
        assert match_package_to_dep("", []) is None
        assert match_package_to_dep("com.x", []) is None


class TestCollectDepsFromDiff:
    def test_collects_deps_from_pom_diff(self) -> None:
        diff = """\
diff --git a/pom.xml b/pom.xml
--- a/pom.xml
+++ b/pom.xml
@@ -0,0 +1,8 @@
+<?xml version="1.0"?>
+<project>
+<dependencies>
+<dependency><groupId>com.google.guava</groupId>
+<artifactId>guava</artifactId>
+<version>33.0.0-jre</version></dependency>
+</dependencies>
+</project>
"""
        deps = collect_deps_from_diff(diff)
        assert BuildFileDep("com.google.guava", "guava", "33.0.0-jre") in deps

    def test_collects_deps_from_sbt_diff(self) -> None:
        diff = """\
diff --git a/build.sbt b/build.sbt
--- a/build.sbt
+++ b/build.sbt
@@ -0,0 +1,2 @@
+libraryDependencies +=
+  "com.google.guava" % "guava" % "33.0.0-jre"
"""
        deps = collect_deps_from_diff(diff)
        assert BuildFileDep("com.google.guava", "guava", "33.0.0-jre") in deps

    def test_ignores_non_build_files(self) -> None:
        diff = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -0,0 +1,2 @@
+<dependency><groupId>x</groupId><artifactId>y</artifactId></dependency>
+"a" % "b" % "c"
"""
        assert collect_deps_from_diff(diff) == []

    def test_invalid_diff_returns_empty(self) -> None:
        assert collect_deps_from_diff("not a diff") == []
