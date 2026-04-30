"""Maven Central doc fetcher (dual-path).

API path: ``https://search.maven.org/solrsearch/select`` JSON for
groupId/artifactId/version. Then fetch javadoc.io HTML for the class
and locate the method's anchor.

Scrape path: ``https://mvnrepository.com/artifact/{group}/{artifact}``
HTML for coordinates, then javadoc.io for the method-level docs.

Both extract: signature, docstring blurb, complexity hints (rare in
javadoc but matched anyway), and the ``@Deprecated`` flag.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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

_SOLR_URL = "https://search.maven.org/solrsearch/select"
_MVNREPO_BASE = "https://mvnrepository.com/artifact"
_JAVADOC_BASE = "https://javadoc.io/doc"

_BIG_O_RE = re.compile(r"\bO\s*\([^)]+\)")
_COMPLEXITY_RE = re.compile(r"(?im)^(?:complexity|performance)\s*[:\-].*$")


class MavenDocsFetcher(DataFetcher[PackageDocs]):
    """Fetch upstream docs for a Java method via Maven Central + javadoc.io."""

    def __init__(
        self,
        client: httpx.Client,
        scrape_hosted_docs: bool = True,
    ) -> None:
        super().__init__(client=client, rate_limiter=None)
        self._scrape_hosted_docs = scrape_hosted_docs

    def fetch_via_api(self, *args: object, **kwargs: object) -> PackageDocs:
        package, qualified_name = _unpack_args(args, kwargs)
        coords = _resolve_coords_via_solr(self._client, package, qualified_name)
        return self._build(
            FetchMethod.API,
            package=package,
            qualified_name=qualified_name,
            coords=coords,
        )

    def fetch_via_scrape(self, *args: object, **kwargs: object) -> PackageDocs:
        package, qualified_name = _unpack_args(args, kwargs)
        coords = _resolve_coords_via_mvnrepo(self._client, package)
        if coords is None:
            raise NotFoundError(
                f"No Maven coordinates found for {package}",
                method=FetchMethod.SCRAPE,
                status_code=404,
            )
        return self._build(
            FetchMethod.SCRAPE,
            package=package,
            qualified_name=qualified_name,
            coords=coords,
        )

    def _build(
        self,
        method: FetchMethod,
        *,
        package: str,
        qualified_name: str,
        coords: _Coords | None,
    ) -> PackageDocs:
        if coords is None:
            return PackageDocs(
                fetched_via=method,
                registry=Registry.MAVEN,
                package=package,
                qualified_name=qualified_name,
            )

        doc_url = _javadoc_url(coords, qualified_name)
        docs = PackageDocs(
            fetched_via=method,
            registry=Registry.MAVEN,
            package=f"{coords.group}:{coords.artifact}",
            version=coords.version,
            qualified_name=qualified_name,
            doc_url=doc_url,
        )
        if self._scrape_hosted_docs and doc_url:
            docs = self._enrich_from_javadoc(docs, doc_url, qualified_name)
        return docs

    def _enrich_from_javadoc(
        self,
        docs: PackageDocs,
        doc_url: str,
        qualified_name: str,
    ) -> PackageDocs:
        try:
            html = self._client.get(doc_url, follow_redirects=True).text
        except httpx.HTTPError:
            logger.debug("javadoc.io fetch failed for %s", doc_url, exc_info=True)
            return docs

        signature, docstring, deprecated, dep_msg = _extract_method_section(html, qualified_name)
        complexity = _extract_complexity(docstring)

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
            doc_url=doc_url,
            summary=docs.summary,
            raw_warnings=docs.raw_warnings,
        )


# --- Coordinate resolution --------------------------------------------------


@dataclass(frozen=True)
class _Coords:
    group: str
    artifact: str
    version: str


def _resolve_coords_via_solr(
    client: httpx.Client, package: str, qualified_name: str
) -> _Coords | None:
    """Use search.maven.org Solr API to find a (group, artifact, version) match."""
    # Try class-level lookup first if we have a FQCN; otherwise package match.
    fqcn = qualified_name.rsplit(".", 1)[0] if "." in qualified_name else ""
    queries: list[dict[str, Any]] = []
    if fqcn:
        queries.append({"q": f"fc:{fqcn}", "rows": 1, "wt": "json"})
    queries.append({"q": f"g:{package}", "rows": 1, "wt": "json"})
    queries.append({"q": f"a:{package.split('.')[-1]}", "rows": 1, "wt": "json"})

    for params in queries:
        response = client.get(_SOLR_URL, params=params)
        if response.status_code != 200:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        docs = payload.get("response", {}).get("docs") or []
        if not docs:
            continue
        first = docs[0]
        group = str(first.get("g") or "")
        artifact = str(first.get("a") or "")
        version = str(first.get("latestVersion") or first.get("v") or "")
        if group and artifact:
            return _Coords(group=group, artifact=artifact, version=version)
    return None


def _resolve_coords_via_mvnrepo(client: httpx.Client, package: str) -> _Coords | None:
    """Fallback: scrape mvnrepository.com search results to guess coordinates."""
    parts = package.split(".")
    if len(parts) < 2:
        return None
    # mvnrepository's URL scheme matches groupId/artifactId — guess the
    # group is the full package and the artifact is the last component.
    group = package
    artifact = parts[-1]
    url = f"{_MVNREPO_BASE}/{group}/{artifact}"
    try:
        response = client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    version_link = soup.select_one("a.vbtn.release")
    version = version_link.get_text(strip=True) if version_link is not None else ""
    return _Coords(group=group, artifact=artifact, version=version)


def _javadoc_url(coords: _Coords, qualified_name: str) -> str:
    if not coords.version:
        base = f"{_JAVADOC_BASE}/{coords.group}/{coords.artifact}/latest"
    else:
        base = f"{_JAVADOC_BASE}/{coords.group}/{coords.artifact}/{coords.version}"
    fqcn = qualified_name.rsplit(".", 1)[0] if "." in qualified_name else qualified_name
    return f"{base}/{fqcn.replace('.', '/')}.html"


def _extract_method_section(html: str, qualified_name: str) -> tuple[str, str, bool, str]:
    """Pull the javadoc method section (signature, doc, deprecation)."""
    method = qualified_name.rsplit(".", 1)[-1]
    soup = BeautifulSoup(html, "html.parser")

    # Modern javadoc emits <section class="detail" id="method(args)"> blocks
    # whose id starts with the method name.
    detail = None
    for section in soup.find_all("section", class_="detail"):
        sec_id = str(section.get("id", ""))
        if sec_id == method or sec_id.startswith(method + "("):
            detail = section
            break

    from bs4 import Tag

    if detail is None:
        # Older javadoc uses <a name="method-...">.
        anchor = soup.find("a", attrs={"name": re.compile(rf"^{re.escape(method)}-")})
        if not isinstance(anchor, Tag):
            return "", "", False, ""
        # The signature/doc lives in the next sibling content.
        detail = anchor.find_parent()

    if not isinstance(detail, Tag):
        return "", "", False, ""

    sig_el = detail.find(["h3", "h4", "div"], class_=re.compile(r"member|signature|method"))
    signature = sig_el.get_text(" ", strip=True) if sig_el is not None else ""

    block_el = detail.find(["div", "section"], class_=re.compile(r"block|description"))
    docstring = block_el.get_text(" ", strip=True) if block_el is not None else ""

    deprecated_el = detail.find(class_=re.compile(r"deprecation"))
    deprecated = deprecated_el is not None
    dep_msg = deprecated_el.get_text(" ", strip=True) if deprecated_el is not None else ""

    return signature, docstring, deprecated, dep_msg


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


def _unpack_args(args: tuple[object, ...], kwargs: dict[str, object]) -> tuple[str, str]:
    package = str(kwargs.get("package", args[0] if args else ""))
    qualified = str(kwargs.get("qualified_name", args[1] if len(args) > 1 else ""))
    if not package:
        msg = "package is required"
        raise ValueError(msg)
    return package, qualified


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
