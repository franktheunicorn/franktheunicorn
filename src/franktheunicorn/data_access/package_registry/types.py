"""Shared types for package_registry doc fetching."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from franktheunicorn.data_access.base import FetchResult


class Registry(enum.StrEnum):
    """Package registry that supplied a :class:`PackageDocs` record."""

    PYPI = "pypi"
    MAVEN = "maven"


@dataclass(frozen=True)
class PackageDocs(FetchResult):
    """Upstream docs for one third-party function/method.

    Fields are best-effort: any can be empty if the registry or hosted
    docs page didn't expose that detail. The api-misuse check tolerates
    missing fields rather than skipping the call entirely — a deprecated
    flag alone is still useful even if no docstring was found.
    """

    registry: Registry = Registry.PYPI
    package: str = ""
    version: str = ""
    qualified_name: str = ""
    signature: str = ""
    docstring: str = ""
    complexity_notes: str = ""  # extracted from "Complexity:", "O(...)" hints
    deprecated: bool = False
    deprecation_message: str = ""
    doc_url: str = ""
    summary: str = ""  # one-line package summary from the registry
    raw_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _CacheKey:
    registry: Registry
    package: str
    version: str
    qualified_name: str
