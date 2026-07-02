"""End-to-end tests for resolve_call_docs."""

from __future__ import annotations

import re

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.config.models import APIMisuseConfig
from franktheunicorn.data_access.package_registry import resolve_call_docs
from franktheunicorn.data_access.package_registry.types import Registry
from franktheunicorn.review.call_extraction.types import CallSite, Language


@pytest.fixture
def cache_db(tmp_path: object) -> str:
    return str(tmp_path) + "/c.sqlite"  # type: ignore[operator]


def _java_call() -> CallSite:
    return CallSite(
        language=Language.JAVA,
        package="com.google.common",
        qualified_name="com.google.common.collect.ImmutableList.copyOf",
        file_path="Foo.java",
        line_number=10,
        snippet="ImmutableList.copyOf(xs);",
    )


def _python_call() -> CallSite:
    return CallSite(
        language=Language.PYTHON,
        package="pandas",
        qualified_name="pandas.DataFrame.apply",
        file_path="foo.py",
        line_number=5,
        snippet="pd.DataFrame.apply(df, fn)",
    )


class TestResolveCallDocs:
    def test_build_file_in_diff_skips_solr_for_java(
        self, cache_db: str, httpx_mock: HTTPXMock
    ) -> None:
        # The diff carries a pom.xml whose group covers the call's package.
        # resolve_call_docs should hand those deps to MavenDocsFetcher,
        # which short-circuits Solr. pytest_httpx fails the test if any
        # response is registered but unused — and conversely, if we issue
        # a request without a matching response it raises. Either failure
        # would flag the regression.
        diff = """\
diff --git a/pom.xml b/pom.xml
--- a/pom.xml
+++ b/pom.xml
@@ -0,0 +1,8 @@
+<?xml version="1.0"?>
+<project>
+<dependencies>
+<dependency><groupId>com.google.common</groupId>
+<artifactId>guava-internal</artifactId>
+<version>33.0.0</version></dependency>
+</dependencies>
+</project>
"""
        config = APIMisuseConfig(enabled=True, scrape_hosted_docs=False)
        docs = resolve_call_docs(
            [_java_call()],
            config,
            cache_db_path=cache_db,
            diff=diff,
        )
        assert len(docs) == 1
        d = docs[0]
        assert d.registry is Registry.MAVEN
        assert d.package == "com.google.common:guava-internal"
        assert d.version == "33.0.0"

    def test_caches_docs_across_calls(self, cache_db: str, httpx_mock: HTTPXMock) -> None:
        # First call hits PyPI; second call should hit the cache (only one
        # PyPI response is registered as non-reusable).
        httpx_mock.add_response(
            url="https://pypi.org/pypi/pandas/json",
            json={
                "info": {
                    "version": "2.0.0",
                    "summary": "Data analysis",
                    "project_urls": {},
                }
            },
        )
        config = APIMisuseConfig(enabled=True, scrape_hosted_docs=False)
        first = resolve_call_docs([_python_call()], config, cache_db_path=cache_db)
        second = resolve_call_docs([_python_call()], config, cache_db_path=cache_db)
        assert len(first) == 1
        assert len(second) == 1
        assert second[0].package == "pandas"
        assert second[0].version == "2.0.0"

    def test_disabled_registry_is_skipped(self, cache_db: str, httpx_mock: HTTPXMock) -> None:
        # Maven disabled → Java site dropped, no Solr request.
        config = APIMisuseConfig(enabled=True, registries=["pypi"], scrape_hosted_docs=False)
        docs = resolve_call_docs([_java_call()], config, cache_db_path=cache_db)
        assert docs == []

    def test_max_calls_per_pr_caps_fanout(self, cache_db: str, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url=re.compile(r".*pypi\.org/pypi/.*/json"),
            json={"info": {"version": "1.0", "summary": "", "project_urls": {}}},
            is_reusable=True,
        )
        config = APIMisuseConfig(
            enabled=True,
            scrape_hosted_docs=False,
            max_calls_per_pr=2,
        )
        sites = [
            CallSite(
                language=Language.PYTHON,
                package=f"pkg{i}",
                qualified_name=f"pkg{i}.func",
                file_path="x.py",
                line_number=i,
                snippet="",
            )
            for i in range(5)
        ]
        docs = resolve_call_docs(sites, config, cache_db_path=cache_db)
        assert len(docs) == 2

    def test_fetch_error_swallowed(self, cache_db: str, httpx_mock: HTTPXMock) -> None:
        # 404 on the PyPI API falls through to the scrape path (dual-path
        # fallback); when that 404s too, the resolver swallows the error and
        # returns an empty list rather than propagate.
        httpx_mock.add_response(
            url="https://pypi.org/pypi/pandas/json",
            status_code=404,
        )
        httpx_mock.add_response(
            url="https://pypi.org/project/pandas/",
            status_code=404,
        )
        config = APIMisuseConfig(enabled=True, scrape_hosted_docs=False)
        docs = resolve_call_docs([_python_call()], config, cache_db_path=cache_db)
        assert docs == []
