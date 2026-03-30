"""Tests for the GitHub clients (mock and real)."""

from __future__ import annotations

import json
from pathlib import Path

from pytest_httpx import HTTPXMock

from franktheunicorn.github.client import GitHubClient
from franktheunicorn.github.mock import MockGitHubClient


class TestMockGitHubClient:
    def test_builtin_demo_data(self, tmp_path: Path) -> None:
        client = MockGitHubClient(tmp_path)
        prs = client.list_pull_requests("apache", "spark")
        assert len(prs) == 3
        assert prs[0]["number"] == 42
        assert prs[0]["user"]["login"] == "alice-dev"

    def test_fixture_file(self, tmp_path: Path) -> None:
        fixture = tmp_path / "testorg_testrepo_pulls.json"
        fixture.write_text(
            json.dumps(
                [
                    {
                        "id": 999,
                        "number": 1,
                        "title": "From fixture",
                        "user": {"login": "fixture-user"},
                        "state": "open",
                        "html_url": "https://example.com/1",
                        "diff_url": "https://example.com/1.diff",
                        "body": "",
                        "labels": [],
                        "requested_reviewers": [],
                        "draft": False,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                ]
            )
        )
        client = MockGitHubClient(tmp_path)
        prs = client.list_pull_requests("testorg", "testrepo")
        assert len(prs) == 1
        assert prs[0]["title"] == "From fixture"

    def test_get_files_default(self, tmp_path: Path) -> None:
        client = MockGitHubClient(tmp_path)
        files = client.get_pull_request_files("apache", "spark", 42)
        assert len(files) == 2
        assert files[0]["filename"] == "README.md"

    def test_get_diff_default(self, tmp_path: Path) -> None:
        client = MockGitHubClient(tmp_path)
        diff = client.get_pull_request_diff("apache", "spark", 42)
        assert "README.md" in diff

    def test_close(self, tmp_path: Path) -> None:
        client = MockGitHubClient(tmp_path)
        client.close()  # Should not raise


class TestGitHubClient:
    def test_list_pull_requests(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls?state=open&per_page=50",
            json=[{"id": 1, "number": 42, "title": "Test PR"}],
        )
        client = GitHubClient(token="fake-token")
        prs = client.list_pull_requests("apache", "spark")
        assert len(prs) == 1
        assert prs[0]["number"] == 42
        client.close()

    def test_get_pull_request_files(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=[{"filename": "README.md", "additions": 1, "deletions": 0}],
        )
        client = GitHubClient(token="fake-token")
        files = client.get_pull_request_files("apache", "spark", 42)
        assert len(files) == 1
        assert files[0]["filename"] == "README.md"
        client.close()

    def test_get_pull_request_diff(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            text="--- a/README.md\n+++ b/README.md\n",
        )
        client = GitHubClient(token="fake-token")
        diff = client.get_pull_request_diff("apache", "spark", 42)
        assert "README.md" in diff
        client.close()

    def test_no_token(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/test/repo/pulls?state=open&per_page=50",
            json=[],
        )
        client = GitHubClient()  # no token
        prs = client.list_pull_requests("test", "repo")
        assert prs == []
        client.close()
