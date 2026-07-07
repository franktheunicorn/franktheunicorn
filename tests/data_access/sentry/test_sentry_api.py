"""Tests for Sentry API fetcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.cache import FileCache
from franktheunicorn.data_access.sentry.fetcher import (
    SENTRY_API_BASE,
    SentryFetcher,
)
from franktheunicorn.data_access.sentry.types import SentryContext, SentryIssue


@pytest.fixture
def sentry_fetcher(tmp_path: Path) -> SentryFetcher:
    cache = FileCache("sentry", cache_dir=tmp_path, ttl_seconds=0)
    return SentryFetcher(cache=cache)


class TestSentryFetchIssuesForFiles:
    def test_fetches_issues_for_single_file(
        self,
        httpx_mock: HTTPXMock,
        sentry_fetcher: SentryFetcher,
        sentry_issues_response: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url=f"{SENTRY_API_BASE}/projects/myorg/myproj/issues/?query=stack.filename%3A%22src%2Futils%2Ftransform.py%22&statsPeriod=24h",
            json=sentry_issues_response,
        )
        result = sentry_fetcher.fetch_issues_for_files(
            "test-token",
            "myorg",
            "myproj",
            ["src/utils/transform.py"],
        )
        assert len(result.issues) == 2
        assert result.project_slug == "myproj"
        assert result.file_paths_queried == ["src/utils/transform.py"]

    def test_parses_issue_fields(
        self,
        httpx_mock: HTTPXMock,
        sentry_fetcher: SentryFetcher,
        sentry_issues_response: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url=f"{SENTRY_API_BASE}/projects/myorg/myproj/issues/?query=stack.filename%3A%22src%2Futils%2Ftransform.py%22&statsPeriod=24h",
            json=sentry_issues_response,
        )
        result = sentry_fetcher.fetch_issues_for_files(
            "test-token",
            "myorg",
            "myproj",
            ["src/utils/transform.py"],
        )
        issue = result.issues[0]
        assert issue.title == ("TypeError: Cannot read property 'map' of undefined")
        assert issue.culprit == "src/utils/transform.py in apply_map"
        assert issue.count == 42
        assert issue.user_count == 15
        assert issue.short_id == "PROJ-A1"

    def test_deduplicates_across_files(
        self,
        httpx_mock: HTTPXMock,
        sentry_fetcher: SentryFetcher,
        sentry_issues_response: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url=f"{SENTRY_API_BASE}/projects/myorg/myproj/issues/?query=stack.filename%3A%22file_a.py%22&statsPeriod=24h",
            json=sentry_issues_response,
        )
        httpx_mock.add_response(
            url=f"{SENTRY_API_BASE}/projects/myorg/myproj/issues/?query=stack.filename%3A%22file_b.py%22&statsPeriod=24h",
            json=sentry_issues_response,
        )
        result = sentry_fetcher.fetch_issues_for_files(
            "test-token",
            "myorg",
            "myproj",
            ["file_a.py", "file_b.py"],
        )
        # Same 2 issues returned for both files, should deduplicate
        assert len(result.issues) == 2

    def test_empty_token_returns_empty_context(
        self,
        sentry_fetcher: SentryFetcher,
    ) -> None:
        result = sentry_fetcher.fetch_issues_for_files(
            "",
            "myorg",
            "myproj",
            ["src/utils/transform.py"],
        )
        assert result.issues == []
        assert result.project_slug == "myproj"
        assert result.file_paths_queried == ["src/utils/transform.py"]

    def test_api_error_returns_empty_issues(
        self,
        httpx_mock: HTTPXMock,
        sentry_fetcher: SentryFetcher,
    ) -> None:
        httpx_mock.add_response(
            url=f"{SENTRY_API_BASE}/projects/myorg/myproj/issues/?query=stack.filename%3A%22failing.py%22&statsPeriod=24h",
            status_code=500,
        )
        result = sentry_fetcher.fetch_issues_for_files(
            "test-token",
            "myorg",
            "myproj",
            ["failing.py"],
        )
        assert result.issues == []

    def test_empty_file_list(
        self,
        sentry_fetcher: SentryFetcher,
    ) -> None:
        result = sentry_fetcher.fetch_issues_for_files(
            "test-token",
            "myorg",
            "myproj",
            [],
        )
        assert result.issues == []


class TestSentryContextMethods:
    def test_to_prompt_context(self) -> None:
        ctx = SentryContext(
            issues=[
                SentryIssue(
                    title="TypeError in handler",
                    culprit="app.views.handler",
                    count=10,
                    user_count=5,
                    first_seen="2024-01-01T00:00:00Z",
                    last_seen="2024-01-20T00:00:00Z",
                    short_id="PROJ-1",
                ),
            ],
            project_slug="myproj",
            file_paths_queried=["app/views.py"],
        )
        output = ctx.to_prompt_context()
        assert "Sentry errors" in output
        assert "TypeError in handler" in output
        assert "10 events" in output
        assert "5 users" in output
        assert "app.views.handler" in output

    def test_to_prompt_context_empty(self) -> None:
        ctx = SentryContext(project_slug="myproj")
        assert ctx.to_prompt_context() == ""

    def test_to_cache_dict(self) -> None:
        ctx = SentryContext(
            issues=[
                SentryIssue(
                    title="Error",
                    culprit="module.func",
                    count=5,
                    user_count=2,
                    first_seen="2024-01-01",
                    last_seen="2024-01-20",
                    short_id="PROJ-1",
                ),
            ],
            project_slug="myproj",
            file_paths_queried=["file.py"],
        )
        d = ctx.to_cache_dict()
        assert d["project_slug"] == "myproj"
        assert len(d["issues"]) == 1  # type: ignore[arg-type]
        assert d["issues"][0]["title"] == "Error"  # type: ignore[index]
        assert isinstance(d["file_paths_queried"], list)

    def test_to_prompt_context_truncates_at_10(self) -> None:
        issues = [
            SentryIssue(
                title=f"Error {i}",
                count=i,
                short_id=f"PROJ-{i}",
                last_seen="2024-01-20",
            )
            for i in range(15)
        ]
        ctx = SentryContext(
            issues=issues,
            project_slug="myproj",
        )
        output = ctx.to_prompt_context()
        assert "and 5 more issues" in output
