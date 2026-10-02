"""Existing PR comments become a +1, not a second statement of the point."""

from __future__ import annotations

import pytest

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.drafter import create_drafts_from_findings
from franktheunicorn.review.prior_comments import (
    PriorComment,
    adjust_for_prior_comments,
    comments_from_review_payload,
    format_prior_comments,
    plus_one_text,
)
from franktheunicorn.review.prompt import build_user_message
from tests.conftest import make_pr_context


def _comment(**kwargs: object) -> PriorComment:
    fields: dict[str, object] = {
        "author": "cloud-fan",
        "body": "This null check returns 0 from getInt, which hides the null.",
        "file_path": "sql/foo.scala",
        "line": 40,
        "url": "https://github.com/apache/spark/pull/1#discussion_r9",
    }
    fields.update(kwargs)
    return PriorComment(**fields)  # type: ignore[arg-type]


class TestAdjustForPriorComments:
    def test_an_agreement_becomes_a_plus_one(self) -> None:
        comment = _comment()
        action, body = adjust_for_prior_comments(
            "sql/foo.scala",
            42,
            "getInt returns 0 on null, so this check hides the null.",
            [comment],
            operator="holdenk",
            seen=set(),
        )

        assert action == "plus_one"
        assert body == plus_one_text(comment)
        assert body.startswith("+1 @cloud-fan")
        assert "discussion_r9" in body

    def test_the_same_line_with_a_different_point_is_kept(self) -> None:
        action, body = adjust_for_prior_comments(
            "sql/foo.scala",
            40,
            "The binding policy is missing on this new SQLConf entry.",
            [_comment()],
            operator="holdenk",
            seen=set(),
        )

        assert action == "keep"
        assert "binding policy" in body

    def test_our_own_comment_drops_the_finding(self) -> None:
        action, _body = adjust_for_prior_comments(
            "sql/foo.scala",
            40,
            "getInt returns 0 on null, so this check hides the null.",
            [_comment(author="holdenk", url="")],
            operator="holdenk",
            seen=set(),
        )

        assert action == "drop"

    def test_a_bot_comment_drops_without_a_plus_one(self) -> None:
        action, _body = adjust_for_prior_comments(
            "sql/foo.scala",
            40,
            "getInt returns 0 on null, so this check hides the null.",
            [_comment(author="coderabbitai[bot]")],
            operator="holdenk",
            seen=set(),
        )

        assert action == "drop"

    def test_a_second_finding_does_not_plus_one_twice(self) -> None:
        comment = _comment()
        seen: set[str] = set()
        body = "getInt returns 0 on null, so this check hides the null."
        first, _ = adjust_for_prior_comments(
            "sql/foo.scala", 40, body, [comment], operator="holdenk", seen=seen
        )
        second, _ = adjust_for_prior_comments(
            "sql/foo.scala", 41, body, [comment], operator="holdenk", seen=seen
        )

        assert first == "plus_one"
        assert second == "drop"

    def test_review_payload_keeps_path_and_author(self) -> None:
        parsed = comments_from_review_payload(
            [
                {
                    "path": "sql/foo.scala",
                    "line": 12,
                    "body": "Why do we need this helper? It is only called once.",
                    "user": {"login": "dongjoon-hyun"},
                    "html_url": "https://example.test/c",
                },
                {"path": "a.py", "body": "nit", "user": {"login": "x"}},
            ]
        )

        assert len(parsed) == 1
        assert parsed[0].author == "dongjoon-hyun"
        assert parsed[0].line == 12

    def test_the_prompt_lists_comments_already_on_the_pr(self) -> None:
        text = format_prior_comments([_comment()])
        message = build_user_message("diff", make_pr_context(prior_comments=text))

        assert "+1 @author" in message
        assert "@cloud-fan" in message
        assert message.index("@cloud-fan") < message.index("diff")


@pytest.mark.django_db
class TestDraftsPlusOne:
    def test_a_duplicate_finding_is_stored_as_a_plus_one(self, db_pr: object) -> None:
        comment = _comment()
        drafts = create_drafts_from_findings(
            db_pr,  # type: ignore[arg-type]
            [
                ReviewFinding(
                    file_path="sql/foo.scala",
                    line_number=40,
                    title="null getInt",
                    body="getInt returns 0 on null, so this check hides the null.",
                    suggestion="use isNullAt",
                )
            ],
            source="llm",
            prior_comments=[comment],
            operator="holdenk",
        )

        assert len(drafts) == 1
        assert drafts[0].comment_body.startswith("+1 @cloud-fan")
        assert drafts[0].suggestion == ""
