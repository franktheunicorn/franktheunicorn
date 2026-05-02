"""Fetch upstream docs for third-party functions called in a PR diff.

Used by the api-misuse review check: given a list of
:class:`CallSite` objects, look up the called function in its package
registry (PyPI, Maven Central) and return :class:`PackageDocs` with
signature, docstring, complexity hints, and deprecation status.
"""

from __future__ import annotations

from franktheunicorn.data_access.package_registry._helpers import format_docs_block
from franktheunicorn.data_access.package_registry.build_files import (
    BuildFileDep,
    collect_deps_from_diff,
    match_package_to_dep,
    parse_build_sbt,
    parse_pom_xml,
)
from franktheunicorn.data_access.package_registry.cache import DocsCache
from franktheunicorn.data_access.package_registry.maven_tree import (
    resolve_deps_from_checkout,
)
from franktheunicorn.data_access.package_registry.resolver import resolve_call_docs
from franktheunicorn.data_access.package_registry.types import PackageDocs, Registry

__all__ = [
    "BuildFileDep",
    "DocsCache",
    "PackageDocs",
    "Registry",
    "collect_deps_from_diff",
    "format_docs_block",
    "match_package_to_dep",
    "parse_build_sbt",
    "parse_pom_xml",
    "resolve_call_docs",
    "resolve_deps_from_checkout",
]
