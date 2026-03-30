"""Contract tests: verify API and scrape paths produce compatible results."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.dependencies.changelog_fetcher import (
    PythonChangelogFetcher,
)
from franktheunicorn.data_access.dependencies.types import (
    ChangelogEntry,
    VersionTransition,
)


@pytest.fixture(params=["api", "scrape"])
def changelog_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    changelog_fetcher: PythonChangelogFetcher,
    requests_transition: VersionTransition,
    pypi_requests_api_json: dict[str, Any],
    github_release_requests_json: dict[str, Any],
    pypi_requests_page_html: str,
    github_release_requests_html: str,
) -> ChangelogEntry:
    """Produce a ChangelogEntry via either API or scrape path."""
    if request.param == "api":
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json=pypi_requests_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/psf/requests/releases/tags/v2.31.0",
            json=github_release_requests_json,
        )
        return changelog_fetcher.fetch_via_api(requests_transition)
    else:
        httpx_mock.add_response(
            url="https://pypi.org/project/requests/",
            text=pypi_requests_page_html,
        )
        httpx_mock.add_response(
            url="https://github.com/psf/requests/releases/tag/v2.31.0",
            text=github_release_requests_html,
        )
        return changelog_fetcher.fetch_via_scrape(requests_transition)


class TestChangelogContract:
    """Both API and scrape paths must produce a valid ChangelogEntry with consistent data."""

    def test_returns_changelog_entry(self, changelog_result: ChangelogEntry) -> None:
        assert isinstance(changelog_result, ChangelogEntry)

    def test_has_package_name(self, changelog_result: ChangelogEntry) -> None:
        assert changelog_result.package_name == "requests"

    def test_has_versions(self, changelog_result: ChangelogEntry) -> None:
        assert changelog_result.old_version == "2.28.0"
        assert changelog_result.new_version == "2.31.0"

    def test_has_release_notes(self, changelog_result: ChangelogEntry) -> None:
        assert "2.31.0" in changelog_result.release_notes

    def test_has_changelog_url(self, changelog_result: ChangelogEntry) -> None:
        assert "github.com" in changelog_result.changelog_url
        assert "v2.31.0" in changelog_result.changelog_url

    def test_has_release_date(self, changelog_result: ChangelogEntry) -> None:
        assert "2023-05-22" in changelog_result.release_date

    def test_detects_deprecations(self, changelog_result: ChangelogEntry) -> None:
        assert changelog_result.deprecations_detected is True

    def test_no_fetch_error(self, changelog_result: ChangelogEntry) -> None:
        assert changelog_result.fetch_error == ""
