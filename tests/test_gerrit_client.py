"""Tests for the Gerrit Code Review API client."""

from __future__ import annotations

import base64
import json

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.backends.base import ReviewBody, ReviewComment
from franktheunicorn.backends.gerrit import (
    GerritClient,
    _decode_gerrit_json,
    _project_name,
    _to_gerrit_comment,
)


def _gerrit(payload: object) -> bytes:
    """Wrap a JSON value in Gerrit's XSSI prefix."""
    return b")]}'\n" + json.dumps(payload).encode()


class TestDecodeGerritJson:
    def test_strips_prefix(self) -> None:
        assert _decode_gerrit_json(b')]}\'\n{"a": 1}') == {"a": 1}

    def test_tolerates_leading_whitespace(self) -> None:
        assert _decode_gerrit_json(b"\n)]}'\n[]") == []

    def test_works_without_prefix(self) -> None:
        assert _decode_gerrit_json(b'{"a": 1}') == {"a": 1}

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError):
            _decode_gerrit_json(b")]}'\nnot-json")


class TestProjectName:
    def test_owner_and_repo_joined(self) -> None:
        assert _project_name("acme", "widget") == "acme%2Fwidget"

    def test_nested_owner(self) -> None:
        assert _project_name("acme/team", "widget") == "acme%2Fteam%2Fwidget"

    def test_empty_owner_uses_repo_only(self) -> None:
        assert _project_name("", "myproject") == "myproject"


class TestToGerritComment:
    def test_inline_single_line(self) -> None:
        wire = _to_gerrit_comment(ReviewComment(path="a.py", body="nit", line=10))
        assert wire == {"message": "nit", "line": 10}

    def test_inline_range_uses_range_object(self) -> None:
        wire = _to_gerrit_comment(ReviewComment(path="a.py", body="multi", line=5, line_end=8))
        assert wire["range"] == {
            "start_line": 5,
            "start_character": 0,
            "end_line": 8,
            "end_character": 0,
        }
        assert "line" not in wire

    def test_left_side_marks_parent(self) -> None:
        wire = _to_gerrit_comment(ReviewComment(path="a.py", body="nit", line=10, side="LEFT"))
        assert wire["side"] == "PARENT"

    def test_no_line_is_file_level(self) -> None:
        wire = _to_gerrit_comment(ReviewComment(path="a.py", body="general"))
        assert wire == {"message": "general"}


class TestGerritClient:
    @pytest.fixture
    def client(self) -> GerritClient:
        c = GerritClient(
            token="http-pass",
            base_url="https://gerrit.test",
            username="alice",
        )
        yield c
        c.close()

    def test_requires_base_url(self) -> None:
        with pytest.raises(ValueError, match="requires base_url"):
            GerritClient(token="t", username="alice")

    def test_anonymous_when_no_credentials(self) -> None:
        c = GerritClient(base_url="https://gerrit.test")
        assert c._authed is False
        assert c._prefix() == ""
        c.close()

    def test_token_with_embedded_username(self) -> None:
        c = GerritClient(token="bob:secret", base_url="https://gerrit.test")
        assert c._authed is True
        assert c._prefix() == "/a"
        c.close()

    def test_uses_authenticated_prefix(self, httpx_mock: HTTPXMock, client: GerritClient) -> None:
        httpx_mock.add_response(
            url="https://gerrit.test/a/accounts/self",
            content=_gerrit({"username": "alice", "name": "Alice"}),
        )
        result = client.get_authenticated_user()
        assert result["login"] == "alice"

    def test_list_pull_requests_translates_status(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        httpx_mock.add_response(
            content=_gerrit(
                [
                    {
                        "_number": 42,
                        "change_id": "Iabc",
                        "status": "NEW",
                        "subject": "fix",
                        "branch": "main",
                        "current_revision": "deadbeef",
                        "owner": {"username": "bob", "name": "Bob"},
                        "labels": {"Code-Review": {}},
                    }
                ]
            ),
        )
        result = client.list_pull_requests("acme", "widget")
        assert len(result) == 1
        pr = result[0]
        assert pr["number"] == 42
        assert pr["state"] == "open"
        assert pr["title"] == "fix"
        assert pr["user"]["login"] == "bob"
        assert pr["labels"] == [{"name": "Code-Review"}]
        assert pr["base"]["ref"] == "main"
        assert pr["head"]["sha"] == "deadbeef"
        assert pr["html_url"].endswith("/c/acme/widget/+/42")

        request = httpx_mock.get_request()
        assert request is not None
        # Authenticated client must hit /a/.
        assert request.url.path == "/a/changes/"
        assert b"project%3Aacme%2Fwidget" in request.url.query
        assert b"status%3Aopen" in request.url.query

    def test_get_pull_request_merged_becomes_closed(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        httpx_mock.add_response(
            content=_gerrit(
                [
                    {
                        "_number": 7,
                        "status": "MERGED",
                        "subject": "ship it",
                        "branch": "main",
                        "owner": {"username": "carol"},
                    }
                ]
            ),
        )
        pr = client.get_pull_request("acme", "widget", 7)
        assert pr["state"] == "closed"

    def test_get_pull_request_missing_raises(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        httpx_mock.add_response(content=_gerrit([]))
        with pytest.raises(LookupError):
            client.get_pull_request("acme", "widget", 9999)

    def test_get_pull_request_files_skips_commit_msg(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        # First call: resolve change.
        httpx_mock.add_response(
            content=_gerrit([{"_number": 1, "current_revision": "rev1", "change_id": "Iabc"}]),
        )
        # Second call: files dict.
        httpx_mock.add_response(
            content=_gerrit(
                {
                    "/COMMIT_MSG": {"status": "M"},
                    "src/a.py": {"status": "A"},
                    "src/b.py": {},  # missing status defaults to modified
                    "src/c.py": {"status": "D"},
                }
            ),
        )
        files = client.get_pull_request_files("acme", "widget", 1)
        assert {(f["filename"], f["status"]) for f in files} == {
            ("src/a.py", "added"),
            ("src/b.py", "modified"),
            ("src/c.py", "removed"),
        }

    def test_get_pull_request_diff_decodes_base64(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        httpx_mock.add_response(
            content=_gerrit([{"_number": 1, "current_revision": "rev1", "change_id": "Iabc"}]),
        )
        diff_text = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
        httpx_mock.add_response(text=base64.b64encode(diff_text.encode()).decode())
        out = client.get_pull_request_diff("acme", "widget", 1)
        assert "diff --git a/x b/x" in out
        assert "+new" in out

    def test_create_review_posts_comments_grouped_by_file(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        # Resolve change.
        httpx_mock.add_response(
            content=_gerrit([{"_number": 1, "current_revision": "rev1", "change_id": "Iabc"}]),
        )
        # Review POST.
        httpx_mock.add_response(
            url="https://gerrit.test/a/changes/1/revisions/rev1/review",
            method="POST",
            content=_gerrit({"labels": {}}),
        )
        # Fetch comments back to map IDs.
        httpx_mock.add_response(
            url="https://gerrit.test/a/changes/1/comments",
            method="GET",
            content=_gerrit(
                {
                    "src/a.py": [{"id": "uuid-a-1", "line": 5, "message": "nit"}],
                    "src/b.py": [{"id": "uuid-b-1", "line": 9, "message": "wat"}],
                }
            ),
        )

        review = ReviewBody(
            event="COMMENT",
            body="overall looks good",
            comments=[
                ReviewComment(path="src/a.py", body="nit", correlation_key="k1", line=5),
                ReviewComment(path="src/b.py", body="wat", correlation_key="k2", line=9),
            ],
        )
        result = client.create_review("acme", "widget", 1, review)
        assert result["id"] == 1
        assert result["comment_ids_by_key"] == {"k1": "uuid-a-1", "k2": "uuid-b-1"}

        review_post = next(
            r
            for r in httpx_mock.get_requests()
            if r.method == "POST" and r.url.path.endswith("/review")
        )
        sent = json.loads(review_post.content)
        assert sent["message"] == "overall looks good"
        assert sent["comments"]["src/a.py"] == [{"message": "nit", "line": 5}]
        assert sent["comments"]["src/b.py"] == [{"message": "wat", "line": 9}]

    def test_create_review_without_comments_skips_id_lookup(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        httpx_mock.add_response(
            content=_gerrit([{"_number": 4, "current_revision": "rev", "change_id": "Iabc"}]),
        )
        httpx_mock.add_response(
            url="https://gerrit.test/a/changes/4/revisions/rev/review",
            method="POST",
            content=_gerrit({}),
        )
        result = client.create_review("acme", "widget", 4, ReviewBody(body="overall only"))
        assert result["id"] == 4
        assert result["comment_ids_by_key"] == {}
        # No GET on /comments should have been issued.
        assert not any(
            r.url.path.endswith("/comments") and r.method == "GET"
            for r in httpx_mock.get_requests()
        )

    def test_get_review_comments_flattens_by_file(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        httpx_mock.add_response(
            content=_gerrit([{"_number": 3, "current_revision": "rev", "change_id": "Iabc"}]),
        )
        httpx_mock.add_response(
            content=_gerrit(
                {
                    "a.py": [{"id": "u1", "message": "x"}],
                    "b.py": [{"id": "u2", "message": "y"}],
                }
            ),
        )
        out = client.get_review_comments("acme", "widget", 3, review_id=0)
        paths = sorted(c["path"] for c in out)
        assert paths == ["a.py", "b.py"]

    def test_get_issue_comments_filters_by_since(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        httpx_mock.add_response(
            content=_gerrit([{"_number": 5, "current_revision": "r", "change_id": "Iabc"}]),
        )
        httpx_mock.add_response(
            content=_gerrit(
                [
                    {
                        "id": "m1",
                        "message": "old",
                        "date": "2025-01-01 00:00:00.000",
                        "author": {"username": "alice", "name": "Alice"},
                    },
                    {
                        "id": "m2",
                        "message": "fresh",
                        "date": "2026-04-01 00:00:00.000",
                        "author": {"username": "bob"},
                    },
                ]
            ),
        )
        out = client.get_issue_comments("acme", "widget", 5, since="2026-01-01")
        assert len(out) == 1
        assert out[0]["body"] == "fresh"
        assert out[0]["user"]["login"] == "bob"

    def test_delete_review_comment_posts_with_reason(
        self, httpx_mock: HTTPXMock, client: GerritClient
    ) -> None:
        httpx_mock.add_response(
            content=_gerrit([{"_number": 9, "current_revision": "rev", "change_id": "Iabc"}]),
        )
        httpx_mock.add_response(
            url="https://gerrit.test/a/changes/9/revisions/rev/comments/abc/delete",
            method="POST",
            content=_gerrit({}),
        )
        client.delete_review_comment("acme", "widget", 9, "abc")
        delete_post = next(
            r
            for r in httpx_mock.get_requests()
            if r.method == "POST" and r.url.path.endswith("/delete")
        )
        sent = json.loads(delete_post.content)
        assert sent["reason"]
