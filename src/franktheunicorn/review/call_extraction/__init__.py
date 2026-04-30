"""Extract external function call sites from a unified diff.

The api-misuse check uses these extractors to identify which third-party
functions a PR is calling, so it can fetch upstream docs and ask the LLM
to flag misuse.

Each language extractor returns a list of :class:`CallSite` objects.
Stdlib and first-party packages are filtered out by the extractors so
downstream code can focus on third-party calls.
"""

from __future__ import annotations

from franktheunicorn.review.call_extraction.types import CallSite, Language

__all__ = ["CallSite", "Language", "extract_calls"]


def extract_calls(diff: str, *, project_package: str = "") -> list[CallSite]:
    """Extract external function call sites from a unified diff.

    Dispatches to per-language extractors based on file extensions seen
    in the diff. Skips stdlib calls and (when ``project_package`` is set)
    first-party calls under that package.
    """
    from franktheunicorn.review.call_extraction.java import extract_java_calls
    from franktheunicorn.review.call_extraction.python import extract_python_calls

    sites: list[CallSite] = []
    sites.extend(extract_python_calls(diff, project_package=project_package))
    sites.extend(extract_java_calls(diff, project_package=project_package))
    return sites
