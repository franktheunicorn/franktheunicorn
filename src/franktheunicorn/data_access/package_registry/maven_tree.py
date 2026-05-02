"""Resolve project dependencies by running the build tool.

Far more accurate than parsing ``pom.xml`` ourselves: Maven and Gradle
do the work of resolving parent POMs, BOMs, properties, and transitive
dependencies before we see the output. We just shell out and parse the
tree.

If the build tool isn't installed or the build is broken, the caller
should fall back to the diff-based parser in :mod:`build_files`.

Results are memoised in-process on ``(checkout_path, build_file_mtime)``
so a worker doesn't reinvoke Maven for every PR in the same repo.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from franktheunicorn.data_access.package_registry.build_files import BuildFileDep

logger = logging.getLogger(__name__)

# In-process cache keyed by (checkout_path_str, build_file_mtime).
_TREE_CACHE: dict[tuple[str, float], list[BuildFileDep]] = {}

_MAVEN_LINE_RE = re.compile(
    r"^\[INFO\]\s*[+\\\-|\s]+"
    r"([\w.-]+):"  # groupId
    r"([\w.-]+):"  # artifactId
    r"[\w.-]+:"  # type (jar, war, etc.)
    r"([\w.\-]+)"  # version
    r"(?::[\w.-]+)?"  # optional scope
    r"\s*$",
)

_GRADLE_LINE_RE = re.compile(
    r"^[+\\\-|\s]+---\s+"
    r"([\w.-]+):"  # group
    r"([\w.-]+):"  # artifact
    r"([\w.\-]+)"  # version
    r"(?:\s*->\s*[\w.\-]+)?"  # version-override arrow (we keep declared version)
    r"(?:\s*\([cnr*]\))?"  # gradle annotations (c=constraint, n=not-resolved, etc.)
    r"\s*$",
)

# Keep the dependency-tree subprocess bounded so a hung build doesn't
# wedge the worker. 60s is enough for first-time downloads on CI runners.
_TIMEOUT_SECONDS = 60


def resolve_deps_from_checkout(repo_path: Path | str | None) -> list[BuildFileDep]:
    """Run ``mvn`` / ``gradle`` in ``repo_path`` and return resolved deps.

    Returns an empty list when ``repo_path`` is missing, no recognised
    build file is present, the build tool isn't on ``$PATH``, or the
    invocation fails. Callers should treat ``[]`` as "no signal" and
    fall back to other dep sources.
    """
    if repo_path is None:
        return []
    root = Path(repo_path)
    if not root.is_dir():
        return []

    pom = root / "pom.xml"
    gradle = root / "build.gradle"
    gradle_kts = root / "build.gradle.kts"

    if pom.is_file():
        return _cached(pom, _run_maven)
    if gradle.is_file():
        return _cached(gradle, _run_gradle)
    if gradle_kts.is_file():
        return _cached(gradle_kts, _run_gradle)
    return []


def _cached(
    build_file: Path,
    runner: Callable[[Path], list[BuildFileDep]],
) -> list[BuildFileDep]:
    try:
        mtime = build_file.stat().st_mtime
    except OSError:
        return []
    key = (str(build_file.parent.resolve()), mtime)
    if key in _TREE_CACHE:
        return _TREE_CACHE[key]
    deps = runner(build_file.parent)
    _TREE_CACHE[key] = deps
    return deps


def _run_maven(repo: Path) -> list[BuildFileDep]:
    if shutil.which("mvn") is None:
        logger.debug("mvn not on PATH; skipping dependency:tree for %s", repo)
        return []
    try:
        result = subprocess.run(
            ["mvn", "-B", "-q", "dependency:tree", "-DoutputType=text"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("mvn dependency:tree failed in %s: %s", repo, exc)
        return []
    if result.returncode != 0:
        logger.info(
            "mvn dependency:tree exited %d in %s; stderr=%s",
            result.returncode,
            repo,
            result.stderr[:500],
        )
        return []
    return _parse_maven_output(result.stdout)


def _run_gradle(repo: Path) -> list[BuildFileDep]:
    """Run ``gradle dependencies`` and parse the tree.

    Prefers the wrapper (``./gradlew``) so we use the project's pinned
    Gradle version. Falls back to a system ``gradle`` if no wrapper is
    present.
    """
    wrapper = repo / "gradlew"
    if wrapper.is_file():
        cmd = [str(wrapper), "dependencies", "--configuration", "runtimeClasspath", "-q"]
    elif shutil.which("gradle") is not None:
        cmd = ["gradle", "dependencies", "--configuration", "runtimeClasspath", "-q"]
    else:
        logger.debug("No gradlew or system gradle for %s", repo)
        return []
    try:
        result = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("gradle dependencies failed in %s: %s", repo, exc)
        return []
    if result.returncode != 0:
        logger.info(
            "gradle dependencies exited %d in %s; stderr=%s",
            result.returncode,
            repo,
            result.stderr[:500],
        )
        return []
    return _parse_gradle_output(result.stdout)


def _parse_maven_output(output: str) -> list[BuildFileDep]:
    deps: list[BuildFileDep] = []
    seen: set[tuple[str, str, str]] = set()
    for line in output.splitlines():
        match = _MAVEN_LINE_RE.match(line)
        if match is None:
            continue
        group, artifact, version = match.group(1), match.group(2), match.group(3)
        key = (group, artifact, version)
        if key in seen:
            continue
        seen.add(key)
        deps.append(BuildFileDep(group=group, artifact=artifact, version=version))
    return deps


def _parse_gradle_output(output: str) -> list[BuildFileDep]:
    deps: list[BuildFileDep] = []
    seen: set[tuple[str, str, str]] = set()
    for line in output.splitlines():
        match = _GRADLE_LINE_RE.match(line)
        if match is None:
            continue
        group, artifact, version = match.group(1), match.group(2), match.group(3)
        key = (group, artifact, version)
        if key in seen:
            continue
        seen.add(key)
        deps.append(BuildFileDep(group=group, artifact=artifact, version=version))
    return deps


def _clear_cache() -> None:
    """Test hook — clear the in-process cache."""
    _TREE_CACHE.clear()
