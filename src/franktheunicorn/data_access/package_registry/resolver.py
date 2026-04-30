"""Top-level helper: ``CallSite`` → cached :class:`PackageDocs`.

Used by the api-misuse review check. Picks the right registry based on
the call's language, consults the on-disk cache first, and falls back
to the appropriate dual-path fetcher on cache miss. Honors the
operator's ``APIMisuseConfig`` (registries allowlist, cache TTL, fetch
timeout, hosted-docs scraping toggle).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from franktheunicorn.data_access.base import FetchError
from franktheunicorn.data_access.package_registry.build_files import (
    BuildFileDep,
    collect_deps_from_diff,
)
from franktheunicorn.data_access.package_registry.cache import DocsCache
from franktheunicorn.data_access.package_registry.maven import MavenDocsFetcher
from franktheunicorn.data_access.package_registry.pypi import PyPIDocsFetcher
from franktheunicorn.data_access.package_registry.types import PackageDocs, Registry
from franktheunicorn.review.call_extraction.types import CallSite, Language

if TYPE_CHECKING:
    from franktheunicorn.config.models import APIMisuseConfig

logger = logging.getLogger(__name__)

_DEFAULT_DB = "data/frank.sqlite3"


def resolve_call_docs(
    sites: list[CallSite],
    config: APIMisuseConfig,
    *,
    cache_db_path: str | Path | None = None,
    client: httpx.Client | None = None,
    diff: str = "",
    build_file_deps: list[BuildFileDep] | None = None,
) -> list[PackageDocs]:
    """Return :class:`PackageDocs` for each call site (best-effort).

    Cache hits short-circuit network requests. Sites whose registry is
    disabled in ``config.registries`` or whose package can't be looked
    up return ``None`` and are dropped from the result.

    When ``diff`` is supplied, ``pom.xml`` / ``build.sbt`` content is
    parsed out of it and used to map Java packages to Maven coordinates
    in preference to the Solr fallback. ``build_file_deps`` lets callers
    supply pre-parsed deps directly (e.g. fetched from the PR's base
    branch) — these are merged with deps discovered in the diff.
    """
    if not sites:
        return []

    enabled = {r.lower() for r in config.registries}
    cache = DocsCache(cache_db_path or _DEFAULT_DB, ttl_days=config.cache_ttl_days)

    owned_client = client is None
    active_client: httpx.Client = (
        client if client is not None else httpx.Client(timeout=config.fetch_timeout_seconds)
    )

    deps: list[BuildFileDep] = []
    if diff:
        deps.extend(collect_deps_from_diff(diff))
    if build_file_deps:
        deps.extend(build_file_deps)

    pypi = PyPIDocsFetcher(active_client, scrape_hosted_docs=config.scrape_hosted_docs)
    maven = MavenDocsFetcher(
        active_client,
        scrape_hosted_docs=config.scrape_hosted_docs,
        build_file_deps=deps,
    )

    results: list[PackageDocs] = []
    try:
        budget = config.max_calls_per_pr
        for site in sites:
            if budget <= 0:
                logger.info("api-misuse: max_calls_per_pr reached, stopping fan-out")
                break

            registry = _registry_for(site)
            if registry is None or registry.value not in enabled:
                continue

            cached = cache.get(
                registry,
                package=site.package,
                qualified_name=site.qualified_name,
            )
            if cached is not None:
                results.append(cached)
                continue

            try:
                if registry is Registry.PYPI:
                    docs = pypi.fetch(site.package, site.qualified_name)
                else:
                    docs = maven.fetch(site.package, site.qualified_name)
            except FetchError as exc:
                logger.debug(
                    "api-misuse: docs fetch failed for %s.%s: %s",
                    site.package,
                    site.qualified_name,
                    exc,
                )
                continue
            except httpx.HTTPError as exc:
                logger.debug(
                    "api-misuse: HTTP error for %s.%s: %s",
                    site.package,
                    site.qualified_name,
                    exc,
                )
                continue

            cache.put(docs)
            results.append(docs)
            budget -= 1
    finally:
        if owned_client:
            active_client.close()

    return results


def _registry_for(site: CallSite) -> Registry | None:
    if site.language is Language.PYTHON:
        return Registry.PYPI
    if site.language is Language.JAVA:
        return Registry.MAVEN
    return None  # type: ignore[unreachable]
