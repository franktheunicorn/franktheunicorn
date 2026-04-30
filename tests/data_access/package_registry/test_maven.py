"""Tests for the Maven docs fetcher (API + scrape paths)."""

from __future__ import annotations

import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError
from franktheunicorn.data_access.package_registry.build_files import BuildFileDep
from franktheunicorn.data_access.package_registry.maven import MavenDocsFetcher
from franktheunicorn.data_access.package_registry.types import Registry


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client(timeout=5.0)


@pytest.fixture
def fetcher(client: httpx.Client) -> MavenDocsFetcher:
    return MavenDocsFetcher(client=client, scrape_hosted_docs=False)


_SOLR_HIT = {
    "response": {
        "docs": [
            {
                "g": "com.google.guava",
                "a": "guava",
                "latestVersion": "33.0.0-jre",
            }
        ]
    }
}


class TestMavenDocsFetcherAPI:
    def test_resolves_coords_via_solr_when_no_build_files(
        self, fetcher: MavenDocsFetcher, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            url=re.compile(r".*search\.maven\.org/solrsearch/select.*"),
            json=_SOLR_HIT,
        )
        docs = fetcher.fetch_via_api(
            "com.google.common", "com.google.common.collect.ImmutableList.copyOf"
        )
        assert docs.fetched_via is FetchMethod.API
        assert docs.registry is Registry.MAVEN
        assert docs.package == "com.google.guava:guava"
        assert docs.version == "33.0.0-jre"
        assert docs.doc_url.startswith("https://javadoc.io/doc/com.google.guava/guava/")

    def test_build_file_match_skips_solr(self, client: httpx.Client, httpx_mock: HTTPXMock) -> None:
        # Build-file deps cover the package — Solr should never be hit.
        # pytest_httpx fails the test if any registered response goes
        # unused, so the absence of an httpx_mock.add_response also
        # asserts no network requests went out.
        deps = [BuildFileDep("com.google.common", "guava-internal", "1.0")]
        f = MavenDocsFetcher(client=client, scrape_hosted_docs=False, build_file_deps=deps)
        docs = f.fetch_via_api(
            "com.google.common", "com.google.common.collect.ImmutableList.copyOf"
        )
        assert docs.package == "com.google.common:guava-internal"
        assert docs.version == "1.0"

    def test_returns_empty_docs_when_no_hits(
        self, fetcher: MavenDocsFetcher, httpx_mock: HTTPXMock
    ) -> None:
        # The resolver tries up to 3 Solr queries; register one reusable response.
        httpx_mock.add_response(
            url=re.compile(r".*search\.maven\.org.*"),
            json={"response": {"docs": []}},
            is_reusable=True,
        )
        docs = fetcher.fetch_via_api("com.fake.pkg", "com.fake.pkg.X.y")
        assert docs.package == "com.fake.pkg"
        assert docs.version == ""
        assert docs.doc_url == ""


class TestMavenDocsFetcherScrape:
    def test_scrape_path_uses_build_file_deps(self, client: httpx.Client) -> None:
        deps = [BuildFileDep("com.example.pkg", "pkg", "1.2.3")]
        f = MavenDocsFetcher(client=client, scrape_hosted_docs=False, build_file_deps=deps)
        docs = f.fetch_via_scrape("com.example.pkg", "com.example.pkg.X.y")
        assert docs.fetched_via is FetchMethod.SCRAPE
        assert docs.version == "1.2.3"
        assert docs.package == "com.example.pkg:pkg"

    def test_scrape_path_raises_when_no_build_file_match(self, fetcher: MavenDocsFetcher) -> None:
        with pytest.raises(NotFoundError):
            fetcher.fetch_via_scrape("com.fake.pkg", "com.fake.pkg.X.y")
