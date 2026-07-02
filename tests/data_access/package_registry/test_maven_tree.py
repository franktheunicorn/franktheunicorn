"""Tests for the Maven / Gradle dependency-tree runner."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from franktheunicorn.data_access.package_registry import maven_tree
from franktheunicorn.data_access.package_registry.build_files import BuildFileDep
from franktheunicorn.data_access.package_registry.maven_tree import (
    _parse_gradle_output,
    _parse_maven_output,
    resolve_deps_from_checkout,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Each test gets a fresh in-process cache."""
    maven_tree._clear_cache()


_MAVEN_OUTPUT = """\
[INFO] Scanning for projects...
[INFO]
[INFO] -----------< com.example:demo >-----------
[INFO] --- maven-dependency-plugin:dependency:tree ---
[INFO] com.example:demo:jar:1.0.0
[INFO] +- com.google.guava:guava:jar:33.0.0-jre:compile
[INFO] |  +- com.google.guava:failureaccess:jar:1.0.2:compile
[INFO] |  \\- com.google.errorprone:error_prone_annotations:jar:2.23.0:compile
[INFO] +- org.apache.commons:commons-lang3:jar:3.14.0:compile
[INFO] \\- org.junit.jupiter:junit-jupiter-api:jar:5.10.0:test
[INFO] BUILD SUCCESS
"""


_GRADLE_OUTPUT = """\
> Task :dependencies

runtimeClasspath - Runtime classpath of source set 'main'.
+--- com.google.guava:guava:33.0.0-jre
|    +--- com.google.guava:failureaccess:1.0.2
|    \\--- org.checkerframework:checker-qual:3.42.0
+--- org.apache.commons:commons-lang3:3.14.0
\\--- io.netty:netty-all:4.1.100.Final
"""


class TestParseMaven:
    def test_extracts_resolved_deps_with_versions(self) -> None:
        deps = _parse_maven_output(_MAVEN_OUTPUT)
        assert BuildFileDep("com.google.guava", "guava", "33.0.0-jre") in deps
        assert BuildFileDep("com.google.guava", "failureaccess", "1.0.2") in deps
        assert BuildFileDep("org.apache.commons", "commons-lang3", "3.14.0") in deps
        # Test scope is included — caller can filter if needed.
        assert BuildFileDep("org.junit.jupiter", "junit-jupiter-api", "5.10.0") in deps

    def test_dedupes_repeated_coords(self) -> None:
        # Same coord appearing twice (e.g. in different branches of the tree)
        # is collapsed.
        output = _MAVEN_OUTPUT + "[INFO] +- com.google.guava:guava:jar:33.0.0-jre:compile\n"
        deps = _parse_maven_output(output)
        guava = [d for d in deps if d.artifact == "guava"]
        assert len(guava) == 1

    def test_ignores_non_dep_lines(self) -> None:
        deps = _parse_maven_output("[INFO] Scanning for projects...\n[INFO] BUILD SUCCESS\n")
        assert deps == []


class TestParseGradle:
    def test_extracts_resolved_deps(self) -> None:
        deps = _parse_gradle_output(_GRADLE_OUTPUT)
        assert BuildFileDep("com.google.guava", "guava", "33.0.0-jre") in deps
        assert BuildFileDep("com.google.guava", "failureaccess", "1.0.2") in deps
        assert BuildFileDep("io.netty", "netty-all", "4.1.100.Final") in deps

    def test_handles_version_override_arrow(self) -> None:
        output = "+--- com.example:foo:1.0 -> 1.1\n"
        deps = _parse_gradle_output(output)
        # Declared version is extracted; the override arrow is recognised
        # but we keep the declared coord so callers see what's in the file.
        assert deps == [BuildFileDep("com.example", "foo", "1.0")]


class TestResolveDepsFromCheckout:
    @pytest.fixture(autouse=True)
    def _trusted_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # These tests exercise the build-tool invocation itself; the
        # trusted-branch guard has its own tests below.
        monkeypatch.setattr(maven_tree, "_is_on_trusted_ref", lambda _repo: True)

    def test_returns_empty_when_no_repo_path(self) -> None:
        assert resolve_deps_from_checkout(None) == []

    def test_returns_empty_when_path_missing(self, tmp_path: Path) -> None:
        assert resolve_deps_from_checkout(tmp_path / "does-not-exist") == []

    def test_returns_empty_when_no_build_file(self, tmp_path: Path) -> None:
        assert resolve_deps_from_checkout(tmp_path) == []

    def test_runs_maven_when_pom_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pom = tmp_path / "pom.xml"
        pom.write_text("<project/>")  # parser never sees this — the tree runner does

        monkeypatch.setattr(maven_tree.shutil, "which", lambda _name: "/usr/bin/mvn")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=_MAVEN_OUTPUT, stderr=""
            )

        monkeypatch.setattr(maven_tree.subprocess, "run", fake_run)
        deps = resolve_deps_from_checkout(tmp_path)
        assert BuildFileDep("com.google.guava", "guava", "33.0.0-jre") in deps

    def test_returns_empty_when_mvn_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        monkeypatch.setattr(maven_tree.shutil, "which", lambda _name: None)
        assert resolve_deps_from_checkout(tmp_path) == []

    def test_returns_empty_when_mvn_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        monkeypatch.setattr(maven_tree.shutil, "which", lambda _name: "/usr/bin/mvn")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(maven_tree.subprocess, "run", fake_run)
        assert resolve_deps_from_checkout(tmp_path) == []

    def test_caches_on_repeat_calls(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")
        monkeypatch.setattr(maven_tree.shutil, "which", lambda _name: "/usr/bin/mvn")

        call_count = {"n": 0}

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            call_count["n"] += 1
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=_MAVEN_OUTPUT, stderr=""
            )

        monkeypatch.setattr(maven_tree.subprocess, "run", fake_run)
        first = resolve_deps_from_checkout(tmp_path)
        second = resolve_deps_from_checkout(tmp_path)
        assert first == second
        assert call_count["n"] == 1, "second call should hit the cache"

    def test_picks_gradle_over_pom_when_only_gradle_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "build.gradle").write_text("// gradle build")
        monkeypatch.setattr(maven_tree.shutil, "which", lambda name: f"/usr/bin/{name}")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else kwargs.get("args", [])
            stdout = _GRADLE_OUTPUT if cmd and "gradle" in cmd[0] else ""
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(maven_tree.subprocess, "run", fake_run)
        deps = resolve_deps_from_checkout(tmp_path)
        assert BuildFileDep("com.google.guava", "guava", "33.0.0-jre") in deps


class TestTrustedRefGuard:
    """Build tools execute checkout-controlled scripts on the host — they
    must never run while the clone is at a PR head."""

    def _write_pom(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>")

    def test_skips_when_detached_head(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write_pom(tmp_path)
        monkeypatch.setattr(maven_tree.shutil, "which", lambda _name: "/usr/bin/mvn")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd[:2] == ["git", "symbolic-ref"]:
                # Detached HEAD → symbolic-ref fails.
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
            raise AssertionError(f"build tool must not run on detached HEAD: {cmd}")

        monkeypatch.setattr(maven_tree.subprocess, "run", fake_run)
        assert resolve_deps_from_checkout(tmp_path) == []

    def test_skips_on_review_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._write_pom(tmp_path)
        monkeypatch.setattr(maven_tree.shutil, "which", lambda _name: "/usr/bin/mvn")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd[:2] == ["git", "symbolic-ref"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="franktheunicorn-review-42\n", stderr=""
                )
            raise AssertionError(f"build tool must not run on a review branch: {cmd}")

        monkeypatch.setattr(maven_tree.subprocess, "run", fake_run)
        assert resolve_deps_from_checkout(tmp_path) == []

    def test_runs_on_default_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._write_pom(tmp_path)
        monkeypatch.setattr(maven_tree.shutil, "which", lambda _name: "/usr/bin/mvn")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd[:2] == ["git", "symbolic-ref"]:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="main\n", stderr=""
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=_MAVEN_OUTPUT, stderr=""
            )

        monkeypatch.setattr(maven_tree.subprocess, "run", fake_run)
        deps = resolve_deps_from_checkout(tmp_path)
        assert BuildFileDep("com.google.guava", "guava", "33.0.0-jre") in deps
