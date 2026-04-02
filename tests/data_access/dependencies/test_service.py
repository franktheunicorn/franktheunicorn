"""Tests for the dependency changelog service layer."""

from __future__ import annotations

import re
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.dependencies.service import (
    detect_and_fetch_changelogs,
)
from franktheunicorn.data_access.github.types import PRDiff, PRFileChange


@pytest.mark.django_db
class TestDetectAndFetchChangelogs:
    """Integration tests for detect_and_fetch_changelogs."""

    def test_detects_and_persists_dependency_changes(
        self,
        httpx_mock: HTTPXMock,
        http_client: Any,
        db_pr: Any,
        requirements_txt_patch: str,
        pypi_requests_api_json: dict[str, Any],
        github_release_requests_json: dict[str, Any],
    ) -> None:
        """End-to-end: parse diff → fetch changelogs → persist to DB."""
        diff = PRDiff(
            pr_number=db_pr.number,
            raw_diff="",
            files=(
                PRFileChange(
                    filename="requirements.txt",
                    status="modified",
                    additions=2,
                    deletions=2,
                    patch=requirements_txt_patch,
                ),
            ),
        )

        # Mock PyPI for both packages
        httpx_mock.add_response(
            url="https://pypi.org/pypi/requests/json",
            json=pypi_requests_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/psf/requests/releases/tags/v2.31.0",
            json=github_release_requests_json,
        )

        # numpy: PyPI API returns a response pointing to GitHub
        httpx_mock.add_response(
            url="https://pypi.org/pypi/numpy/json",
            json={
                "info": {
                    "name": "numpy",
                    "project_urls": {"source": "https://github.com/numpy/numpy"},
                    "home_page": None,
                }
            },
        )
        # numpy: all tag candidates return 404
        httpx_mock.add_response(
            url=re.compile(r"https://api\.github\.com/repos/numpy/numpy/releases/tags/.*"),
            status_code=404,
            is_reusable=True,
        )

        results = detect_and_fetch_changelogs(db_pr, diff, http_client)

        assert len(results) == 2

        # Check the requests result
        req_change = next(r for r in results if r.package_name == "requests")
        assert req_change.old_version == "2.28.0"
        assert req_change.new_version == "2.31.0"
        assert req_change.ecosystem == "python"
        assert req_change.changelog_url != ""
        assert req_change.changelog_text != ""

        # Check it's persisted
        assert db_pr.dependency_changes.count() == 2

    def test_skips_existing_entries(
        self,
        httpx_mock: HTTPXMock,
        http_client: Any,
        db_pr: Any,
        requirements_txt_patch: str,
        pypi_requests_api_json: dict[str, Any],
        github_release_requests_json: dict[str, Any],
    ) -> None:
        """Should not re-fetch changelogs for already-recorded dependencies."""
        from tests.factories import DependencyChangeFactory

        # Pre-create a dependency change
        DependencyChangeFactory(
            pull_request=db_pr,
            package_name="requests",
            ecosystem="python",
            old_version="2.28.0",
            new_version="2.31.0",
            source_file="requirements.txt",
        )

        diff = PRDiff(
            pr_number=db_pr.number,
            raw_diff="",
            files=(
                PRFileChange(
                    filename="requirements.txt",
                    status="modified",
                    patch=requirements_txt_patch,
                ),
            ),
        )

        # Only numpy should be fetched
        httpx_mock.add_response(
            url="https://pypi.org/pypi/numpy/json",
            json={
                "info": {
                    "name": "numpy",
                    "project_urls": {},
                    "home_page": None,
                }
            },
        )

        results = detect_and_fetch_changelogs(db_pr, diff, http_client)

        # Only 1 new result (numpy), requests was skipped
        assert len(results) == 1
        assert results[0].package_name == "numpy"

    def test_handles_empty_diff(
        self,
        http_client: Any,
        db_pr: Any,
    ) -> None:
        diff = PRDiff(pr_number=db_pr.number, raw_diff="", files=())
        results = detect_and_fetch_changelogs(db_pr, diff, http_client)
        assert len(results) == 0
