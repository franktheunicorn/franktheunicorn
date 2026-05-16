"""Tests for TemplateFetcher (API + scrape paths)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError
from franktheunicorn.data_access.github.template_fetcher import TemplateFetcher, _decode_contents
from franktheunicorn.data_access.github.types import PRTemplateSummary


@pytest.fixture
def fetcher(http_client: httpx.Client) -> TemplateFetcher:
    return TemplateFetcher(client=http_client)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class TestTemplateFetcherAPI:
    def test_finds_github_template(self, httpx_mock: HTTPXMock, fetcher: TemplateFetcher) -> None:
        template = "## Summary\n\n## Changes\n"
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/contents/.github/pull_request_template.md",
            json={"encoding": "base64", "content": _b64(template)},
        )
        result = fetcher.fetch_via_api("apache", "spark")
        assert isinstance(result, PRTemplateSummary)
        assert result.text == template
        assert result.fetched_via == FetchMethod.API

    def test_falls_through_to_second_candidate(
        self, httpx_mock: HTTPXMock, fetcher: TemplateFetcher
    ) -> None:
        template = "## What changed?\n"
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/contents/.github/pull_request_template.md",
            status_code=404,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/contents/.github/PULL_REQUEST_TEMPLATE.md",
            json={"encoding": "base64", "content": _b64(template)},
        )
        result = fetcher.fetch_via_api("apache", "spark")
        assert result.text == template

    def test_no_template_returns_empty(
        self, httpx_mock: HTTPXMock, fetcher: TemplateFetcher
    ) -> None:
        for path in (
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            "docs/pull_request_template.md",
            "pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE",
        ):
            httpx_mock.add_response(
                url=f"https://api.github.com/repos/apache/spark/contents/{path}",
                status_code=404,
            )
        result = fetcher.fetch_via_api("apache", "spark")
        assert result.text == ""
        assert result.fetched_via == FetchMethod.API

    def test_directory_listing_fetches_first_md(
        self, httpx_mock: HTTPXMock, fetcher: TemplateFetcher
    ) -> None:
        template = "## Directory template\n"
        for path in (
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            "docs/pull_request_template.md",
            "pull_request_template.md",
        ):
            httpx_mock.add_response(
                url=f"https://api.github.com/repos/apache/spark/contents/{path}",
                status_code=404,
            )
        file_url = "https://api.github.com/repos/apache/spark/contents/.github/PULL_REQUEST_TEMPLATE/default.md"
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/contents/.github/PULL_REQUEST_TEMPLATE",
            json=[{"name": "default.md", "url": file_url}],
        )
        httpx_mock.add_response(
            url=file_url,
            json={"encoding": "base64", "content": _b64(template)},
        )
        result = fetcher.fetch_via_api("apache", "spark")
        assert result.text == template


class TestTemplateFetcherScrape:
    def test_finds_raw_template(self, httpx_mock: HTTPXMock, fetcher: TemplateFetcher) -> None:
        template = "## Summary\n"
        httpx_mock.add_response(
            url="https://raw.githubusercontent.com/apache/spark/HEAD/.github/pull_request_template.md",
            text=template,
            status_code=200,
        )
        result = fetcher.fetch_via_scrape("apache", "spark")
        assert result.text == template.strip()
        assert result.fetched_via == FetchMethod.SCRAPE

    def test_falls_through_candidates(self, httpx_mock: HTTPXMock, fetcher: TemplateFetcher) -> None:
        template = "## Root template\n"
        for path in (
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            "docs/pull_request_template.md",
        ):
            httpx_mock.add_response(
                url=f"https://raw.githubusercontent.com/apache/spark/HEAD/{path}",
                status_code=404,
            )
        httpx_mock.add_response(
            url="https://raw.githubusercontent.com/apache/spark/HEAD/pull_request_template.md",
            text=template,
            status_code=200,
        )
        result = fetcher.fetch_via_scrape("apache", "spark")
        assert result.text == template.strip()

    def test_no_template_returns_empty(self, httpx_mock: HTTPXMock, fetcher: TemplateFetcher) -> None:
        for path in (
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            "docs/pull_request_template.md",
            "pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE/PULL_REQUEST_TEMPLATE.md",
            ".github/PULL_REQUEST_TEMPLATE/default.md",
        ):
            httpx_mock.add_response(
                url=f"https://raw.githubusercontent.com/apache/spark/HEAD/{path}",
                status_code=404,
            )
        result = fetcher.fetch_via_scrape("apache", "spark")
        assert result.text == ""


class TestDecodeContents:
    def test_base64_decoding(self) -> None:
        data = {"encoding": "base64", "content": _b64("hello world")}
        assert _decode_contents(data) == "hello world"

    def test_base64_with_newlines_in_content(self) -> None:
        # GitHub API splits base64 content with newlines every 60 chars
        raw = base64.b64encode(b"hello world").decode()
        wrapped = raw[:6] + "\n" + raw[6:]
        data = {"encoding": "base64", "content": wrapped}
        assert _decode_contents(data) == "hello world"

    def test_plain_content(self) -> None:
        data = {"encoding": "none", "content": "plaintext"}
        assert _decode_contents(data) == "plaintext"

    def test_empty_content(self) -> None:
        assert _decode_contents({"encoding": "base64", "content": ""}) == ""
        assert _decode_contents({}) == ""
