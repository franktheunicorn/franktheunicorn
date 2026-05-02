"""Shared types for diff-based call-site extraction."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Language(enum.StrEnum):
    """Language of the file a call site was extracted from."""

    PYTHON = "python"
    JAVA = "java"


@dataclass(frozen=True)
class CallSite:
    """A single external function/method call observed in a PR diff.

    ``package`` is the distribution name on the relevant registry (e.g.
    ``pandas`` for PyPI, ``com.google.guava:guava`` for Maven). ``qualified_name``
    is the dotted path within that package (e.g. ``DataFrame.apply``,
    ``ImmutableList.copyOf``).
    """

    language: Language
    package: str
    qualified_name: str
    file_path: str
    line_number: int
    snippet: str
