"""Tests for the GitHub client."""

from __future__ import annotations

import datetime

import httpx
import respx

from franktheunicorn.github_client import GitHubClient, GitHubPR

FAKE_PR = {
    "number": 101,
    "title": "Add feature",
    "user": {"login": "alice"},
    "state": "open",
    "html_url": "https://github.com/owner/repo/pull/101",
    "body": "This adds a new feature.",
    "labels": [{"name": "enhancement"}],
    "requested_reviewers": [{"login": "bob"}],
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-02T00:00:00Z",
}


def test_github_pr_properties():
    pr = GitHubPR(FAKE_PR)
    assert pr.number == 101
    assert pr.title == "Add feature"
    assert pr.author_login == "alice"
    assert pr.state == "open"
    assert pr.html_url == "https://github.com/owner/repo/pull/101"
    assert pr.body == "This adds a new feature."
    assert pr.labels == ["enhancement"]
    assert pr.requested_reviewers == ["bob"]
    assert isinstance(pr.created_at, datetime.datetime)
    assert isinstance(pr.updated_at, datetime.datetime)


def test_github_pr_missing_fields():
    pr = GitHubPR({})
    assert pr.number == 0
    assert pr.title == ""
    assert pr.author_login == ""
    assert pr.labels == []
    assert pr.requested_reviewers == []
    assert pr.created_at is None
    assert pr.updated_at is None


@respx.mock
def test_list_open_prs_success():
    respx.get("https://api.github.com/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(200, json=[FAKE_PR])
    )
    client = GitHubClient(token="fake")
    prs = client.list_open_prs("owner/repo")
    assert len(prs) == 1
    assert prs[0].number == 101


@respx.mock
def test_list_open_prs_http_error():
    respx.get("https://api.github.com/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    client = GitHubClient(token="fake")
    prs = client.list_open_prs("owner/repo")
    assert prs == []


@respx.mock
def test_list_pr_files():
    respx.get("https://api.github.com/repos/owner/repo/pulls/101/files").mock(
        return_value=httpx.Response(
            200, json=[{"filename": "src/core/engine.py"}, {"filename": "tests/test_engine.py"}]
        )
    )
    client = GitHubClient(token="fake")
    files = client.list_pr_files("owner/repo", 101)
    assert "src/core/engine.py" in files
    assert "tests/test_engine.py" in files


@respx.mock
def test_list_pr_files_error():
    respx.get("https://api.github.com/repos/owner/repo/pulls/999/files").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    client = GitHubClient(token="fake")
    files = client.list_pr_files("owner/repo", 999)
    assert files == []


@respx.mock
def test_get_issue_comments():
    respx.get("https://api.github.com/repos/owner/repo/issues/101/comments").mock(
        return_value=httpx.Response(
            200, json=[{"user": {"login": "franktheunicorn"}, "body": "@franktheunicorn PTAL"}]
        )
    )
    client = GitHubClient(token="fake")
    comments = client.get_issue_comments("owner/repo", 101)
    assert len(comments) == 1
    assert comments[0]["user"]["login"] == "franktheunicorn"


@respx.mock
def test_get_contributors():
    respx.get("https://api.github.com/repos/owner/repo/contributors").mock(
        return_value=httpx.Response(200, json=[{"login": "alice"}, {"login": "bob"}])
    )
    client = GitHubClient(token="fake")
    contribs = client.get_contributors("owner/repo")
    assert "alice" in contribs
    assert "bob" in contribs
