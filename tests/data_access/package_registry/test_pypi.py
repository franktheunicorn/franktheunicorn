"""Tests for the PyPI docs fetcher (API + scrape paths)."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError
from franktheunicorn.data_access.package_registry.pypi import PyPIDocsFetcher
from franktheunicorn.data_access.package_registry.types import Registry


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client(timeout=5.0)


@pytest.fixture
def fetcher(client: httpx.Client) -> PyPIDocsFetcher:
    return PyPIDocsFetcher(client=client, scrape_hosted_docs=False)


_PYPI_JSON = {
    "info": {
        "version": "2.0.0",
        "summary": "Python data analysis library",
        "project_urls": {
            "Documentation": "https://pandas.pydata.org/docs/",
            "Homepage": "https://pandas.pydata.org/",
        },
    }
}


class TestPyPIDocsFetcherAPI:
    def test_fetches_metadata_via_api(
        self, fetcher: PyPIDocsFetcher, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            url="https://pypi.org/pypi/pandas/json",
            json=_PYPI_JSON,
        )
        docs = fetcher.fetch_via_api("pandas", "pandas.DataFrame.apply")
        assert docs.registry is Registry.PYPI
        assert docs.fetched_via is FetchMethod.API
        assert docs.package == "pandas"
        assert docs.version == "2.0.0"
        assert docs.qualified_name == "pandas.DataFrame.apply"
        assert docs.summary == "Python data analysis library"
        assert docs.doc_url == "https://pandas.pydata.org/docs/"

    def test_404_raises_not_found(self, fetcher: PyPIDocsFetcher, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://pypi.org/pypi/no-such-pkg/json",
            status_code=404,
        )
        with pytest.raises(NotFoundError):
            fetcher.fetch_via_api("no-such-pkg", "anything")

    def test_extracts_function_section_from_docs_page(
        self, client: httpx.Client, httpx_mock: HTTPXMock
    ) -> None:
        fetcher = PyPIDocsFetcher(client=client, scrape_hosted_docs=True)
        httpx_mock.add_response(
            url="https://pypi.org/pypi/pandas/json",
            json=_PYPI_JSON,
        )
        # Sphinx-style docs page with a <dt id="..."> anchor. The docstring
        # uses "Deprecated since" rather than the .. directive form because
        # BeautifulSoup collapses the docstring to a single line.
        html = """
<html><body>
<dl>
<dt id="pandas.DataFrame.apply">DataFrame.apply(func, axis=0)</dt>
<dd>Apply a function along an axis. Has complexity O(N*M). Deprecated since 1.0.</dd>
</dl>
</body></html>
"""
        httpx_mock.add_response(
            url="https://pandas.pydata.org/docs/",
            text=html,
        )
        docs = fetcher.fetch_via_api("pandas", "pandas.DataFrame.apply")
        assert "DataFrame.apply" in docs.signature
        assert "Apply a function along an axis" in docs.docstring
        assert "O(N*M)" in docs.complexity_notes
        assert docs.deprecated is True


class TestPyPIDocsFetcherScrape:
    def test_scrape_path_falls_back_to_project_page(
        self, fetcher: PyPIDocsFetcher, httpx_mock: HTTPXMock
    ) -> None:
        html = """
<html><body>
<h1 class="package-header__name">pandas 2.0.0</h1>
<p class="package-description__summary">Data analysis</p>
<ul class="vertical-tabs__list"></ul>
</body></html>
"""
        httpx_mock.add_response(
            url="https://pypi.org/project/pandas/",
            text=html,
        )
        docs = fetcher.fetch_via_scrape("pandas", "pandas.DataFrame.apply")
        assert docs.fetched_via is FetchMethod.SCRAPE
        assert docs.version == "2.0.0"
        assert docs.summary == "Data analysis"

    def test_scrape_404_raises_not_found(
        self, fetcher: PyPIDocsFetcher, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            url="https://pypi.org/project/missing/",
            status_code=404,
        )
        with pytest.raises(NotFoundError):
            fetcher.fetch_via_scrape("missing", "missing.func")
