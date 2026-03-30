"""Tests for PythonChangelogFetcher (API + scrape paths)."""

from __future__ import annotations

import re
from typing import Any

from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.dependencies.changelog_fetcher import (
    PythonChangelogFetcher,
)
from franktheunicorn.data_access.dependencies.types import (
    Ecosystem,
    VersionTransition,
)


class TestPythonChangelogFetcherAPI:
    """Tests for the API path."""

    def test_fetches_changelog_via_api(
        self,
        httpx_mock: HTTPXMock,
        changelog_fetcher: PythonChangelogFetcher,
        requests_transition: VersionTransition,
        pypi_requests_api_json: dict[str, Any],
        github_release_requests_json: dict[str, Any],
    ) -> None:
        # Mock PyPI API
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json=pypi_requests_api_json,
        )
        # Mock GitHub Releases API (first candidate: v2.31.0)
        httpx_mock.add_response(
            url="https://api.github.com/repos/psf/requests/releases/tags/v2.31.0",
            json=github_release_requests_json,
        )

        result = changelog_fetcher.fetch_via_api(requests_transition)

        assert result.fetched_via == FetchMethod.API
        assert result.package_name == "requests"
        assert result.old_version == "2.28.0"
        assert result.new_version == "2.31.0"
        assert "2.31.0" in result.release_notes
        assert result.changelog_url == "https://github.com/psf/requests/releases/tag/v2.31.0"
        assert result.repository_url == "https://github.com/psf/requests"
        assert result.release_date == "2023-05-22T18:30:00Z"
        assert result.deprecations_detected is True  # "deprecated" in release notes
        assert result.fetch_error == ""

    def test_handles_pypi_error(
        self,
        httpx_mock: HTTPXMock,
        changelog_fetcher: PythonChangelogFetcher,
        requests_transition: VersionTransition,
    ) -> None:
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            status_code=500,
        )
        result = changelog_fetcher.fetch_via_api(requests_transition)
        assert result.fetch_error != ""
        assert result.package_name == "requests"

    def test_handles_no_github_release(
        self,
        httpx_mock: HTTPXMock,
        changelog_fetcher: PythonChangelogFetcher,
        requests_transition: VersionTransition,
        pypi_requests_api_json: dict[str, Any],
    ) -> None:
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json=pypi_requests_api_json,
        )
        # All tag candidates return 404
        httpx_mock.add_response(
            url=re.compile(r"https://api\.github\.com/repos/psf/requests/releases/tags/.*"),
            status_code=404,
            is_reusable=True,
        )

        result = changelog_fetcher.fetch_via_api(requests_transition)
        assert result.fetch_error == "No GitHub release found"
        assert result.changelog_url != ""  # Falls back to source URL

    def test_detects_breaking_changes(
        self,
        httpx_mock: HTTPXMock,
        changelog_fetcher: PythonChangelogFetcher,
        pypi_requests_api_json: dict[str, Any],
        github_release_numpy_json: dict[str, Any],
    ) -> None:
        transition = VersionTransition(
            package_name="numpy",
            old_version="1.21.0",
            new_version="1.24.0",
            ecosystem=Ecosystem.PYTHON,
            source_file="requirements.txt",
        )
        # Mock PyPI with a fake numpy response pointing to GitHub
        pypi_json = {
            "info": {
                "name": "numpy",
                "project_urls": {"source": "https://github.com/numpy/numpy"},
                "home_page": None,
            }
        }
        httpx_mock.add_response(
            url="https://pypi.org/pypi/numpy/json",
            json=pypi_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/numpy/numpy/releases/tags/v1.24.0",
            json=github_release_numpy_json,
        )

        result = changelog_fetcher.fetch_via_api(transition)
        assert result.breaking_changes_detected is True
        assert result.deprecations_detected is True


class TestPythonChangelogFetcherScrape:
    """Tests for the scrape path."""

    def test_fetches_changelog_via_scrape(
        self,
        httpx_mock: HTTPXMock,
        changelog_fetcher: PythonChangelogFetcher,
        requests_transition: VersionTransition,
        pypi_requests_page_html: str,
        github_release_requests_html: str,
    ) -> None:
        # Mock PyPI project page
        httpx_mock.add_response(
            url="https://pypi.org/project/requests/",
            text=pypi_requests_page_html,
        )
        # Mock GitHub release page (first candidate: v2.31.0)
        httpx_mock.add_response(
            url="https://github.com/psf/requests/releases/tag/v2.31.0",
            text=github_release_requests_html,
        )

        result = changelog_fetcher.fetch_via_scrape(requests_transition)

        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.package_name == "requests"
        assert "2.31.0" in result.release_notes
        assert result.changelog_url == "https://github.com/psf/requests/releases/tag/v2.31.0"
        assert result.release_date == "2023-05-22T18:30:00Z"
        assert result.deprecations_detected is True

    def test_handles_pypi_404(
        self,
        httpx_mock: HTTPXMock,
        changelog_fetcher: PythonChangelogFetcher,
        requests_transition: VersionTransition,
    ) -> None:
        httpx_mock.add_response(
            url="https://pypi.org/project/requests/",
            status_code=404,
        )
        result = changelog_fetcher.fetch_via_scrape(requests_transition)
        assert "not found" in result.fetch_error.lower()


class TestPythonChangelogFetcherFallback:
    """Tests for the unified fetch() method."""

    def test_api_returns_result_with_error_on_pypi_failure(
        self,
        httpx_mock: HTTPXMock,
        changelog_fetcher: PythonChangelogFetcher,
        requests_transition: VersionTransition,
    ) -> None:
        """API path catches PyPI errors internally and returns a result with fetch_error."""
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            status_code=500,
        )
        # fetch() calls fetch_via_api, which catches the error internally
        # and returns a ChangelogEntry with fetch_error set
        result = changelog_fetcher.fetch(requests_transition)
        assert result.package_name == "requests"
        assert result.fetch_error != ""

    def test_api_path_returns_successfully(
        self,
        httpx_mock: HTTPXMock,
        changelog_fetcher: PythonChangelogFetcher,
        requests_transition: VersionTransition,
        pypi_requests_api_json: dict[str, Any],
        github_release_requests_json: dict[str, Any],
    ) -> None:
        """fetch() uses API path when it succeeds."""
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json=pypi_requests_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/psf/requests/releases/tags/v2.31.0",
            json=github_release_requests_json,
        )
        result = changelog_fetcher.fetch(requests_transition)
        assert result.package_name == "requests"
        assert result.fetch_error == ""
        assert result.release_notes != ""
