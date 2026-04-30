"""PyPI doc fetcher (dual-path).

API path: ``https://pypi.org/pypi/{package}/json`` for version + project
URLs + summary. If the project links to a hosted docs site (readthedocs
or similar), fetch the HTML page and try to extract the function's
section.

Scrape path: same docs URL discovery, but starting from the user-facing
``https://pypi.org/project/{package}/`` HTML page when the JSON API is
rate-limited or unavailable.

Both paths attempt to extract:
- a one-line signature (from a ``<dt>`` like
  ``pandas.DataFrame.apply(func, axis=0, ...)``)
- the docstring blurb that follows
- complexity hints (any line starting with "Complexity" or containing
  big-O notation)
- deprecation flags (``.. deprecated::`` / "Deprecated since")
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    DataFetcher,
    FetchMethod,
    NotFoundError,
)
from franktheunicorn.data_access.package_registry.types import (
    PackageDocs,
    Registry,
)

logger = logging.getLogger(__name__)

_PYPI_JSON_BASE = "https://pypi.org/pypi"
_PYPI_PROJECT_BASE = "https://pypi.org/project"

_DOCS_URL_KEYS: tuple[str, ...] = (
    "documentation",
    "docs",
    "documentation, the",
    "homepage",
    "home-page",
)

_BIG_O_RE = re.compile(r"\bO\s*\([^)]+\)")
_COMPLEXITY_RE = re.compile(r"(?im)^(?:complexity|time complexity|performance)\s*[:\-].*$")
_DEPRECATED_RE = re.compile(r"(?im)\b(?:deprecated since|\.\.\s*deprecated::|@deprecated)\b.*$")


class PyPIDocsFetcher(DataFetcher[PackageDocs]):
    """Fetch upstream docs for a Python function via PyPI + readthedocs."""

    def __init__(
        self,
        client: httpx.Client,
        scrape_hosted_docs: bool = True,
    ) -> None:
        super().__init__(client=client, rate_limiter=None)
        self._scrape_hosted_docs = scrape_hosted_docs

    def fetch_via_api(self, *args: object, **kwargs: object) -> PackageDocs:
        package, qualified_name = _unpack_args(args, kwargs)
        url = f"{_PYPI_JSON_BASE}/{package}/json"
        response = self._client.get(url, headers={"Accept": "application/json"})
        if response.status_code == 404:
            raise NotFoundError(
                f"Package not found on PyPI: {package}",
                method=FetchMethod.API,
                status_code=404,
            )
        response.raise_for_status()
        meta = response.json()

        version = _safe_version(meta)
        summary = _safe_summary(meta)
        docs_url = _resolve_docs_url(meta) or f"{_PYPI_PROJECT_BASE}/{package}/"

        docs = PackageDocs(
            fetched_via=FetchMethod.API,
            registry=Registry.PYPI,
            package=package,
            version=version,
            qualified_name=qualified_name,
            doc_url=docs_url,
            summary=summary,
        )
        if self._scrape_hosted_docs and docs_url:
            docs = self._enrich_from_html(docs, docs_url, qualified_name)
        return docs

    def fetch_via_scrape(self, *args: object, **kwargs: object) -> PackageDocs:
        package, qualified_name = _unpack_args(args, kwargs)
        project_url = f"{_PYPI_PROJECT_BASE}/{package}/"
        try:
            response = self._scrape_get(project_url)
        except NotFoundError:
            raise

        soup = BeautifulSoup(response.text, "html.parser")
        version = _scrape_version(soup)
        summary = _scrape_summary(soup)
        docs_url = _scrape_docs_url(soup) or project_url

        docs = PackageDocs(
            fetched_via=FetchMethod.SCRAPE,
            registry=Registry.PYPI,
            package=package,
            version=version,
            qualified_name=qualified_name,
            doc_url=docs_url,
            summary=summary,
        )
        if self._scrape_hosted_docs and docs_url and docs_url != project_url:
            docs = self._enrich_from_html(docs, docs_url, qualified_name)
        return docs

    def _enrich_from_html(
        self,
        docs: PackageDocs,
        docs_url: str,
        qualified_name: str,
    ) -> PackageDocs:
        try:
            html = self._client.get(docs_url, follow_redirects=True).text
        except httpx.HTTPError:
            logger.debug("Hosted docs fetch failed for %s", docs_url, exc_info=True)
            return docs

        section = _find_function_section(html, qualified_name)
        if not section:
            return docs

        signature, docstring = _split_signature_and_body(section)
        complexity = _extract_complexity(docstring)
        deprecated, dep_msg = _detect_deprecation(docstring)

        return PackageDocs(
            fetched_via=docs.fetched_via,
            fetched_at=docs.fetched_at,
            registry=docs.registry,
            package=docs.package,
            version=docs.version,
            qualified_name=docs.qualified_name,
            signature=signature,
            docstring=_truncate(docstring, 1500),
            complexity_notes=complexity,
            deprecated=deprecated,
            deprecation_message=dep_msg,
            doc_url=docs_url,
            summary=docs.summary,
            raw_warnings=docs.raw_warnings,
        )


def _unpack_args(args: tuple[object, ...], kwargs: dict[str, object]) -> tuple[str, str]:
    """Resolve (package, qualified_name) from ``DataFetcher.fetch`` *args/**kwargs."""
    package = str(kwargs.get("package", args[0] if args else ""))
    qualified = str(kwargs.get("qualified_name", args[1] if len(args) > 1 else ""))
    if not package:
        msg = "package is required"
        raise ValueError(msg)
    return package, qualified


def _safe_version(meta: dict[str, Any]) -> str:
    info = meta.get("info") or {}
    return str(info.get("version") or "")


def _safe_summary(meta: dict[str, Any]) -> str:
    info = meta.get("info") or {}
    return str(info.get("summary") or "")


def _resolve_docs_url(meta: dict[str, Any]) -> str:
    info = meta.get("info") or {}
    project_urls = info.get("project_urls") or {}
    if isinstance(project_urls, dict):
        for key, value in project_urls.items():
            if isinstance(value, str) and key.lower() in _DOCS_URL_KEYS:
                return value
    home = info.get("home_page")
    if isinstance(home, str):
        return home
    return ""


def _scrape_version(soup: BeautifulSoup) -> str:
    header = soup.find("h1", class_="package-header__name")
    if header is None:
        return ""
    text = header.get_text(strip=True)
    parts = text.rsplit(" ", 1)
    return parts[1] if len(parts) == 2 else ""


def _scrape_summary(soup: BeautifulSoup) -> str:
    p = soup.find("p", class_="package-description__summary")
    return p.get_text(strip=True) if p is not None else ""


def _scrape_docs_url(soup: BeautifulSoup) -> str:
    from bs4 import Tag

    sidebar = soup.find("ul", class_="vertical-tabs__list")
    if not isinstance(sidebar, Tag):
        return ""
    for a in sidebar.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if any(key in text for key in ("documentation", "docs", "homepage")):
            return str(a["href"])
    return ""


def _find_function_section(html: str, qualified_name: str) -> str:
    """Best-effort: return the HTML chunk surrounding the function's anchor."""
    if not qualified_name:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    # Sphinx-style function defs: <dt id="package.module.func">...</dt><dd>...</dd>
    dt = soup.find("dt", id=qualified_name)
    if dt is None:
        # Try the leaf attribute name.
        leaf = qualified_name.rsplit(".", 1)[-1]
        candidates = soup.find_all("dt", id=True)
        for cand in candidates:
            cand_id = str(cand.get("id", ""))
            if cand_id.endswith("." + leaf) or cand_id == leaf:
                dt = cand
                break

    if dt is None:
        return ""

    dd = dt.find_next_sibling("dd")
    parts = [dt.get_text(" ", strip=True)]
    if dd is not None:
        parts.append(dd.get_text(" ", strip=True))
    return "\n\n".join(parts)


def _split_signature_and_body(section: str) -> tuple[str, str]:
    lines = [line for line in section.splitlines() if line.strip()]
    if not lines:
        return "", ""
    return lines[0].strip(), "\n".join(lines[1:]).strip()


def _extract_complexity(text: str) -> str:
    if not text:
        return ""
    parts: list[str] = []
    parts.extend(match.group(0).strip() for match in _COMPLEXITY_RE.finditer(text))
    parts.extend(match.group(0) for match in _BIG_O_RE.finditer(text))
    deduped: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(p)
    return "; ".join(deduped)


def _detect_deprecation(text: str) -> tuple[bool, str]:
    if not text:
        return False, ""
    match = _DEPRECATED_RE.search(text)
    if match is None:
        return False, ""
    return True, match.group(0).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
