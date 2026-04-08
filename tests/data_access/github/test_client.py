"""Tests for the GitHub clients (mock and real)."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.github.client import GitHubClient, infer_github_username
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

    def test_get_authenticated_user(self, tmp_path: Path) -> None:
        client = MockGitHubClient(tmp_path)
        user = client.get_authenticated_user()
        assert user["login"] == "mock-user"

    def test_close(self, tmp_path: Path) -> None:
        client = MockGitHubClient(tmp_path)
        client.close()  # Should not raise


class TestGitHubClient:
    @pytest.fixture
    def github_client(self) -> Generator[GitHubClient, None, None]:
        client = GitHubClient(token="fake-token")
        yield client
        client.close()

    def test_list_pull_requests(self, github_client: GitHubClient, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls?state=open&per_page=50",
            json=[{"id": 1, "number": 42, "title": "Test PR"}],
        )
        prs = github_client.list_pull_requests("apache", "spark")
        assert len(prs) == 1
        assert prs[0]["number"] == 42

    def test_get_pull_request_files(
        self, github_client: GitHubClient, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=[{"filename": "README.md", "additions": 1, "deletions": 0}],
        )
        files = github_client.get_pull_request_files("apache", "spark", 42)
        assert len(files) == 1
        assert files[0]["filename"] == "README.md"

    def test_get_pull_request_diff(
        self, github_client: GitHubClient, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            text="--- a/README.md\n+++ b/README.md\n",
        )
        diff = github_client.get_pull_request_diff("apache", "spark", 42)
        assert "README.md" in diff

    def test_get_authenticated_user(
        self, github_client: GitHubClient, httpx_mock: HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/user",
            json={"login": "testuser", "id": 12345, "type": "User"},
        )
        user = github_client.get_authenticated_user()
        assert user["login"] == "testuser"

    def test_no_token(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/test/repo/pulls?state=open&per_page=50",
            json=[],
        )
        client = GitHubClient()  # no token
        prs = client.list_pull_requests("test", "repo")
        assert prs == []
        client.close()


class TestInferGitHubUsername:
    def test_successful_inference(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/user",
            json={"login": "holdenk", "id": 1, "type": "User"},
        )
        assert infer_github_username("ghp_valid_token") == "holdenk"

    def test_invalid_token_returns_empty(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/user",
            status_code=401,
        )
        assert infer_github_username("ghp_bad_token") == ""

    def test_network_error_returns_empty(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_exception(httpx.ConnectError("Connection refused"))
        assert infer_github_username("ghp_any_token") == ""

    def test_empty_token_returns_empty(self) -> None:
        assert infer_github_username("") == ""

    def test_missing_login_field_returns_empty(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/user",
            json={"id": 1, "type": "User"},
        )
        assert infer_github_username("ghp_valid_token") == ""
