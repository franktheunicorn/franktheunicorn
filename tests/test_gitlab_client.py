"""Tests for the GitLab API client."""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.backends.base import ReviewBody, ReviewComment
from franktheunicorn.backends.gitlab import (
    GitLabClient,
    _build_gitlab_discussion,
    _normalize_base_url,
    _normalize_mr,
    _project_id,
)


def test_project_id_url_encodes() -> None:
    assert _project_id("acme", "widget") == "acme%2Fwidget"
    assert _project_id("acme/sub", "widget") == "acme%2Fsub%2Fwidget"


def test_normalize_base_url_appends_api_v4() -> None:
    assert _normalize_base_url("https://gitlab.com") == "https://gitlab.com/api/v4"
    assert _normalize_base_url("https://gitlab.com/api/v4") == "https://gitlab.com/api/v4"


class TestNormalizeMr:
    def test_iid_becomes_number(self) -> None:
        out = _normalize_mr({"iid": 42, "id": 9999, "state": "opened"})
        assert out["number"] == 42

    def test_state_opened_becomes_open(self) -> None:
        assert _normalize_mr({"state": "opened"})["state"] == "open"

    def test_state_merged_becomes_closed(self) -> None:
        assert _normalize_mr({"state": "merged"})["state"] == "closed"

    def test_author_username_becomes_login(self) -> None:
        out = _normalize_mr({"author": {"username": "alice"}})
        assert out["user"]["login"] == "alice"

    def test_description_becomes_body(self) -> None:
        out = _normalize_mr({"description": "hello"})
        assert out["body"] == "hello"

    def test_web_url_becomes_html_url(self) -> None:
        out = _normalize_mr({"web_url": "https://gitlab.com/o/r/-/merge_requests/3"})
        assert out["html_url"] == "https://gitlab.com/o/r/-/merge_requests/3"
        assert out["diff_url"].endswith(".diff")

    def test_string_labels_become_objects(self) -> None:
        out = _normalize_mr({"labels": ["bug", "feat"]})
        assert out["labels"] == [{"name": "bug"}, {"name": "feat"}]

    def test_merge_status_becomes_mergeable(self) -> None:
        assert _normalize_mr({"merge_status": "can_be_merged"})["mergeable"] is True
        assert _normalize_mr({"merge_status": "cannot_be_merged"})["mergeable"] is False

    def test_diff_refs_extracted(self) -> None:
        out = _normalize_mr(
            {
                "diff_refs": {"base_sha": "B", "start_sha": "S", "head_sha": "H"},
                "target_branch": "main",
                "source_branch": "feature",
            }
        )
        assert out["base"]["sha"] == "B"
        assert out["base"]["ref"] == "main"
        assert out["head"]["sha"] == "H"
        assert out["head"]["ref"] == "feature"

    def test_reviewers_become_requested_reviewers(self) -> None:
        out = _normalize_mr({"reviewers": [{"username": "rev1"}, {"username": "rev2"}]})
        assert out["requested_reviewers"] == [{"login": "rev1"}, {"login": "rev2"}]


class TestBuildDiscussion:
    def test_inline_right_side(self) -> None:
        comment = ReviewComment(path="a.py", body="nit", line=10)
        disc = _build_gitlab_discussion(comment, base_sha="B", start_sha="S", head_sha="H")
        assert disc is not None
        assert disc["body"] == "nit"
        pos = disc["position"]
        assert pos["new_line"] == 10
        assert pos["new_path"] == "a.py"
        assert pos["base_sha"] == "B"
        assert pos["head_sha"] == "H"
        assert pos["position_type"] == "text"

    def test_inline_left_side(self) -> None:
        comment = ReviewComment(path="a.py", body="nit", line=10, side="LEFT")
        disc = _build_gitlab_discussion(comment, base_sha="B", start_sha="S", head_sha="H")
        assert disc is not None
        assert disc["position"]["old_line"] == 10

    def test_range_uses_end_line(self) -> None:
        comment = ReviewComment(path="a.py", body="multi", line=10, line_end=15)
        disc = _build_gitlab_discussion(comment, base_sha="B", start_sha="S", head_sha="H")
        assert disc is not None
        assert disc["position"]["new_line"] == 15

    def test_missing_shas_returns_none(self) -> None:
        comment = ReviewComment(path="a.py", body="nit", line=10)
        assert _build_gitlab_discussion(comment, base_sha="", start_sha="", head_sha="") is None

    def test_no_line_becomes_general_note(self) -> None:
        comment = ReviewComment(path="a.py", body="general")
        disc = _build_gitlab_discussion(comment, base_sha="B", start_sha="S", head_sha="H")
        assert disc == {"body": "general"}


class TestGitLabClient:
    @pytest.fixture
    def client(self) -> GitLabClient:
        c = GitLabClient(token="glpat-test", base_url="https://gitlab.test")
        yield c
        c.close()

    def test_uses_private_token_header(self, httpx_mock: HTTPXMock, client: GitLabClient) -> None:
        httpx_mock.add_response(json={"username": "alice"})
        client.get_authenticated_user()
        request = httpx_mock.get_request()
        assert request is not None
        assert request.headers["private-token"] == "glpat-test"

    def test_get_authenticated_user_maps_username(
        self, httpx_mock: HTTPXMock, client: GitLabClient
    ) -> None:
        httpx_mock.add_response(json={"username": "alice", "id": 1})
        result = client.get_authenticated_user()
        assert result["login"] == "alice"

    def test_list_pull_requests_translates_state(
        self, httpx_mock: HTTPXMock, client: GitLabClient
    ) -> None:
        httpx_mock.add_response(
            url="https://gitlab.test/api/v4/projects/o%2Fr/merge_requests?state=opened&per_page=50",
            json=[
                {
                    "iid": 1,
                    "title": "t",
                    "state": "opened",
                    "author": {"username": "alice"},
                    "web_url": "https://gitlab.test/o/r/-/merge_requests/1",
                    "labels": ["bug"],
                }
            ],
        )
        result = client.list_pull_requests("o", "r")
        assert result[0]["number"] == 1
        assert result[0]["state"] == "open"
        assert result[0]["user"]["login"] == "alice"
        assert result[0]["labels"] == [{"name": "bug"}]

    def test_get_pull_request_files(self, httpx_mock: HTTPXMock, client: GitLabClient) -> None:
        httpx_mock.add_response(
            json={
                "changes": [
                    {"old_path": "a.py", "new_path": "a.py"},
                    {"old_path": "b.py", "new_path": "b.py", "new_file": True},
                    {"old_path": "c.py", "new_path": "c.py", "deleted_file": True},
                ]
            }
        )
        result = client.get_pull_request_files("o", "r", 1)
        assert [(f["filename"], f["status"]) for f in result] == [
            ("a.py", "modified"),
            ("b.py", "added"),
            ("c.py", "removed"),
        ]

    def test_get_pull_request_diff_synthesizes_unified_diff(
        self, httpx_mock: HTTPXMock, client: GitLabClient
    ) -> None:
        httpx_mock.add_response(
            json={
                "changes": [
                    {
                        "old_path": "a.py",
                        "new_path": "a.py",
                        "diff": "@@ -1 +1 @@\n-old\n+new",
                    }
                ]
            }
        )
        diff = client.get_pull_request_diff("o", "r", 1)
        assert "diff --git a/a.py b/a.py" in diff
        assert "--- a/a.py" in diff
        assert "+++ b/a.py" in diff
        assert "+new" in diff

    def test_create_review_posts_body_and_inline_discussion(
        self, httpx_mock: HTTPXMock, client: GitLabClient
    ) -> None:
        # Body note.
        httpx_mock.add_response(
            url="https://gitlab.test/api/v4/projects/o%2Fr/merge_requests/1/notes",
            method="POST",
            json={"id": 100},
        )
        # Get MR for SHAs.
        httpx_mock.add_response(
            url="https://gitlab.test/api/v4/projects/o%2Fr/merge_requests/1",
            method="GET",
            json={
                "iid": 1,
                "diff_refs": {"base_sha": "B", "start_sha": "S", "head_sha": "H"},
            },
        )
        # Discussion for inline comment.
        httpx_mock.add_response(
            url="https://gitlab.test/api/v4/projects/o%2Fr/merge_requests/1/discussions",
            method="POST",
            json={"id": "d1", "notes": [{"id": 200}]},
        )

        review = ReviewBody(
            event="COMMENT",
            body="overall",
            comments=[ReviewComment(path="a.py", body="nit", line=5)],
        )
        result = client.create_review("o", "r", 1, review)
        assert result["id"] == 100
        assert result["_inline_note_ids"] == [200]

        # Verify discussion payload.
        disc_post = next(
            r
            for r in httpx_mock.get_requests()
            if r.method == "POST" and r.url.path.endswith("/discussions")
        )
        sent = json.loads(disc_post.content)
        assert sent["body"] == "nit"
        assert sent["position"]["new_line"] == 5
        assert sent["position"]["base_sha"] == "B"

    def test_create_review_drops_inline_when_shas_missing(
        self, httpx_mock: HTTPXMock, client: GitLabClient
    ) -> None:
        httpx_mock.add_response(
            url="https://gitlab.test/api/v4/projects/o%2Fr/merge_requests/1/notes",
            method="POST",
            json={"id": 100},
        )
        httpx_mock.add_response(
            url="https://gitlab.test/api/v4/projects/o%2Fr/merge_requests/1",
            method="GET",
            json={"iid": 1},  # no diff_refs
        )

        review = ReviewBody(
            body="overall",
            comments=[ReviewComment(path="a.py", body="nit", line=5)],
        )
        result = client.create_review("o", "r", 1, review)
        assert result["_inline_note_ids"] == []
        # No discussion request should have been made.
        for r in httpx_mock.get_requests():
            assert not r.url.path.endswith("/discussions"), "should not have posted discussion"

    def test_get_issue_comments_filters_inline_notes(
        self, httpx_mock: HTTPXMock, client: GitLabClient
    ) -> None:
        httpx_mock.add_response(
            json=[
                {"id": 1, "body": "ok", "author": {"username": "alice"}},
                {
                    "id": 2,
                    "body": "inline",
                    "author": {"username": "bob"},
                    "position": {"new_line": 5},
                },
            ]
        )
        result = client.get_issue_comments("o", "r", 1)
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["user"]["login"] == "alice"

    def test_get_issue_comments_translates_since(
        self, httpx_mock: HTTPXMock, client: GitLabClient
    ) -> None:
        httpx_mock.add_response(json=[])
        client.get_issue_comments("o", "r", 1, since="2026-01-01T00:00:00Z")
        request = httpx_mock.get_request()
        assert request is not None
        assert b"updated_after=" in request.url.query

    def test_delete_review_comment_uses_pr_number(
        self, httpx_mock: HTTPXMock, client: GitLabClient
    ) -> None:
        httpx_mock.add_response(
            url="https://gitlab.test/api/v4/projects/o%2Fr/merge_requests/7/notes/123",
            method="DELETE",
            status_code=204,
        )
        client.delete_review_comment("o", "r", 7, 123)
