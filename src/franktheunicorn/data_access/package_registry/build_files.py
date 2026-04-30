"""Resolve Java packages to Maven coordinates from project build files.

The Solr-based resolver in :mod:`maven` works for well-known artifacts
but is unreliable for shaded jars, internal repos, and the long tail of
Java packages where ``groupId`` does not match the FQCN prefix. When
the PR diff contains a ``pom.xml`` or ``build.sbt``, we have a much
better source of truth: the project's own dependency declarations.

This module:

1. Parses Maven ``pom.xml`` and SBT ``build.sbt`` content (no full-fidelity
   parser — just a regex/element pass that covers the common shapes).
2. Builds an in-memory index from a Java package prefix to a list of
   ``(group, artifact, version)`` candidates.
3. Picks the candidate whose ``groupId`` is the longest matching prefix
   of the queried package.

When no build file is supplied or no match is found, :mod:`maven` falls
back to its Solr query as before.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildFileDep:
    """A single (groupId, artifactId, version) declared in a build file."""

    group: str
    artifact: str
    version: str = ""


_POM_NS_RE = re.compile(r"\{[^}]+\}")
_SBT_DEP_RE = re.compile(
    # Matches: "group" % "artifact" % "version" with optional %% (Scala suffix)
    # and optional trailing modifiers (% "test", classifier, etc.).
    r'"([^"]+)"\s*%%?\s*"([^"]+)"\s*%\s*"([^"]+)"',
)


def parse_pom_xml(content: str) -> list[BuildFileDep]:
    """Parse Maven ``pom.xml`` content and return its ``<dependency>`` entries.

    Resolves ``${...}`` property references against the file's own
    ``<properties>`` block. Unresolved references are left as-is.
    """
    if not content.strip():
        return []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        logger.debug("pom.xml failed to parse", exc_info=True)
        return []

    properties = _collect_pom_properties(root)
    deps: list[BuildFileDep] = []
    for dep in _findall_local(root, "dependency"):
        group = _resolve_props(_findtext_local(dep, "groupId"), properties)
        artifact = _resolve_props(_findtext_local(dep, "artifactId"), properties)
        version = _resolve_props(_findtext_local(dep, "version"), properties)
        if group and artifact:
            deps.append(BuildFileDep(group=group, artifact=artifact, version=version))
    return deps


def parse_build_sbt(content: str) -> list[BuildFileDep]:
    """Parse SBT ``build.sbt`` content and return its libraryDependencies entries.

    Only the inline ``"group" % "artifact" % "version"`` form is recognised.
    Variable-expansion forms (``Dependencies.guava``) are out of scope; the
    Solr fallback handles those.
    """
    deps: list[BuildFileDep] = []
    for match in _SBT_DEP_RE.finditer(content):
        group, artifact, version = match.group(1), match.group(2), match.group(3)
        deps.append(BuildFileDep(group=group, artifact=artifact, version=version))
    return deps


def match_package_to_dep(java_package: str, deps: list[BuildFileDep]) -> BuildFileDep | None:
    """Return the dep whose ``group`` is the longest prefix of ``java_package``.

    Falls back to artifact-name match (case-insensitive, normalised on
    ``-`` and ``.``) when no group prefix matches — covers the common
    case where ``groupId`` is stylised but the artifactId mirrors a
    package segment.
    """
    if not java_package or not deps:
        return None

    prefix_match: BuildFileDep | None = None
    prefix_match_len = -1
    for dep in deps:
        if not dep.group:
            continue
        prefix_hit = java_package == dep.group or java_package.startswith(dep.group + ".")
        if prefix_hit and len(dep.group) > prefix_match_len:
            prefix_match = dep
            prefix_match_len = len(dep.group)
    if prefix_match is not None:
        return prefix_match

    package_segments = {
        seg.replace("-", "").replace(".", "").lower() for seg in java_package.split(".")
    }
    for dep in deps:
        artifact_norm = dep.artifact.replace("-", "").replace(".", "").lower()
        if artifact_norm in package_segments:
            return dep
    return None


def collect_deps_from_diff(diff: str) -> list[BuildFileDep]:
    """Parse pom.xml / build.sbt content out of a unified diff.

    Reads added lines for any ``pom.xml`` or ``build.sbt`` file and runs
    the appropriate parser on the reconstructed post-image. Useful when
    the diff itself contains the build-file declarations the api-misuse
    check needs to map calls to coordinates.
    """
    from unidiff import PatchSet  # type: ignore[import-untyped]

    try:
        patch = PatchSet(diff)
    except Exception:
        logger.debug("Failed to parse diff for build-file collection", exc_info=True)
        return []

    deps: list[BuildFileDep] = []
    for pf in patch:
        path = (getattr(pf, "path", "") or getattr(pf, "target_file", "")).lower()
        added = "\n".join(line.value.rstrip("\n") for hunk in pf for line in hunk if line.is_added)
        if not added:
            continue
        if path.endswith("pom.xml"):
            deps.extend(parse_pom_xml(added))
        elif path.endswith("build.sbt"):
            deps.extend(parse_build_sbt(added))
    return deps


# --- Helpers ---------------------------------------------------------------


def _collect_pom_properties(root: ET.Element) -> dict[str, str]:
    properties: dict[str, str] = {}
    for props in _findall_local(root, "properties"):
        for child in list(props):
            tag = _strip_ns(child.tag)
            if child.text is not None:
                properties[tag] = child.text.strip()
    return properties


def _resolve_props(value: str, properties: dict[str, str]) -> str:
    if not value or "${" not in value:
        return value
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: properties.get(m.group(1), m.group(0)),
        value,
    )


def _strip_ns(tag: str) -> str:
    return _POM_NS_RE.sub("", tag)


def _findall_local(elem: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in elem.iter() if _strip_ns(child.tag) == name]


def _findtext_local(elem: ET.Element, name: str) -> str:
    for child in elem.iter():
        if _strip_ns(child.tag) == name and child.text is not None:
            return child.text.strip()
    return ""
