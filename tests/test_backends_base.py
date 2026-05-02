"""Tests for the forge-agnostic ForgeClient ABC and helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from franktheunicorn.backends.base import (
    ForgeClient,
    ReviewBody,
    ReviewComment,
    infer_username,
)


def test_review_comment_defaults() -> None:
    comment = ReviewComment(path="a.py", body="x")
    assert comment.correlation_key == ""
    assert comment.line is None
    assert comment.line_end is None
    assert comment.side == "RIGHT"


def test_review_body_defaults() -> None:
    review = ReviewBody()
    assert review.event == "COMMENT"
    assert review.body == ""
    assert review.comments == []


def test_review_body_holds_comments() -> None:
    c = ReviewComment(path="a.py", body="x", line=5)
    review = ReviewBody(event="REQUEST_CHANGES", body="overall", comments=[c])
    assert review.comments[0].line == 5


def test_infer_username_returns_login() -> None:
    client = MagicMock(spec=ForgeClient)
    client.get_authenticated_user.return_value = {"login": "alice"}
    assert infer_username(client) == "alice"


def test_infer_username_returns_empty_on_error() -> None:
    client = MagicMock(spec=ForgeClient)
    client.get_authenticated_user.side_effect = RuntimeError("network down")
    assert infer_username(client) == ""


def test_infer_username_returns_empty_on_missing_field() -> None:
    client = MagicMock(spec=ForgeClient)
    client.get_authenticated_user.return_value = {"id": 1}
    assert infer_username(client) == ""


def test_infer_username_returns_empty_when_login_not_string() -> None:
    client = MagicMock(spec=ForgeClient)
    client.get_authenticated_user.return_value = {"login": 42}
    assert infer_username(client) == ""


def test_forge_client_is_abstract() -> None:
    """ForgeClient cannot be instantiated directly — needs all methods implemented."""
    import pytest

    class Incomplete(ForgeClient):
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_forge_client_concrete_subclass_works() -> None:
    """A subclass implementing every method instantiates fine."""

    class Stub(ForgeClient):
        def list_pull_requests(
            self, owner: str, repo: str, state: str = "open"
        ) -> list[dict[str, Any]]:
            return []

        def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
            return {}

        def get_pull_request_files(
            self, owner: str, repo: str, pr_number: int
        ) -> list[dict[str, Any]]:
            return []

        def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
            return ""

        def create_review(
            self, owner: str, repo: str, pr_number: int, review: ReviewBody
        ) -> dict[str, Any]:
            return {}

        def get_review_comments(
            self, owner: str, repo: str, pr_number: int, review_id: int
        ) -> list[dict[str, Any]]:
            return []

        def get_issue_comments(
            self,
            owner: str,
            repo: str,
            issue_number: int,
            since: str | None = None,
        ) -> list[dict[str, Any]]:
            return []

        def delete_review_comment(
            self, owner: str, repo: str, pr_number: int, comment_id: int
        ) -> None:
            pass

        def get_authenticated_user(self) -> dict[str, Any]:
            return {"login": "stub"}

        def close(self) -> None:
            pass

    stub = Stub()
    assert infer_username(stub) == "stub"
