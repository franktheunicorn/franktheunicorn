"""Fetch upstream docs for third-party functions called in a PR diff.

Used by the api-misuse review check: given a list of
:class:`CallSite` objects, look up the called function in its package
registry (PyPI, Maven Central) and return :class:`PackageDocs` with
signature, docstring, complexity hints, and deprecation status.
"""

from __future__ import annotations

from franktheunicorn.data_access.package_registry.cache import DocsCache
from franktheunicorn.data_access.package_registry.resolver import resolve_call_docs
from franktheunicorn.data_access.package_registry.types import PackageDocs, Registry

__all__ = ["DocsCache", "PackageDocs", "Registry", "resolve_call_docs"]
