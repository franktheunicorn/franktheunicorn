"""Reviewer standing: committer (bold + *), org member (bold), or plain."""

from __future__ import annotations

from franktheunicorn.dashboard.templatetags.score_filters import reviewer
from franktheunicorn.scoring.reviewer_standing import (
    STANDING_COMMITTER,
    STANDING_MEMBER,
    STANDING_NONE,
    Reviewer,
    committer_set,
    merge_intent_reviewer,
    reviewers,
    standing_for,
)

STATES = {
    "cloud-fan": {"state": "APPROVED", "author_association": "MEMBER"},
    "someone": {"state": "APPROVED", "author_association": "CONTRIBUTOR"},
    "owner": {"state": "APPROVED", "author_association": "OWNER"},
    "old-row": {"state": "APPROVED"},
}


class TestStandingFor:
    def test_committer_beats_member(self) -> None:
        known = committer_set(["Cloud-Fan"])
        assert standing_for("cloud-fan", "MEMBER", known) == STANDING_COMMITTER

    def test_committer_without_association(self) -> None:
        # A committer with a private org membership reads as CONTRIBUTOR.
        assert standing_for("HyukjinKwon", "CONTRIBUTOR", committer_set(["hyukjinkwon"])) == (
            STANDING_COMMITTER
        )

    def test_member_and_owner(self) -> None:
        assert standing_for("x", "member", frozenset()) == STANDING_MEMBER
        assert standing_for("x", "OWNER", frozenset()) == STANDING_MEMBER

    def test_everyone_else(self) -> None:
        for assoc in ("COLLABORATOR", "CONTRIBUTOR", "NONE", ""):
            assert standing_for("x", assoc, frozenset()) == STANDING_NONE

    def test_committer_set_ignores_blanks(self) -> None:
        assert committer_set(["", "  ", " Bob "]) == frozenset({"bob"})
        assert committer_set(None) == frozenset()


class TestReviewers:
    def test_reads_association_from_review_states(self) -> None:
        got = reviewers(["cloud-fan", "someone", "owner", "old-row", ""], STATES, ["owner"])
        assert got == [
            Reviewer("cloud-fan", STANDING_MEMBER),
            Reviewer("someone", STANDING_NONE),
            Reviewer("owner", STANDING_COMMITTER),
            Reviewer("old-row", STANDING_NONE),
        ]

    def test_login_case_drift(self) -> None:
        assert reviewers(["Cloud-Fan"], STATES, [])[0].standing == STANDING_MEMBER

    def test_non_mapping_states(self) -> None:
        assert reviewers(["bob"], None, ["bob"]) == [Reviewer("bob", STANDING_COMMITTER)]
        assert reviewers(["bob"], {"bob": "junk"}, []) == [Reviewer("bob", STANDING_NONE)]


class TestMergeIntentReviewer:
    def test_none_without_login(self) -> None:
        assert merge_intent_reviewer("", "MEMBER", STATES, []) is None

    def test_own_association_wins(self) -> None:
        got = merge_intent_reviewer("someone", "MEMBER", STATES, [])
        assert got == Reviewer("someone", STANDING_MEMBER)

    def test_falls_back_to_review_states(self) -> None:
        got = merge_intent_reviewer("cloud-fan", "", STATES, [])
        assert got == Reviewer("cloud-fan", STANDING_MEMBER)


class TestReviewerFilter:
    def test_committer_is_bold_with_star(self) -> None:
        html = reviewer(Reviewer("holdenk", STANDING_COMMITTER))
        assert "<strong" in html and "holdenk</strong>*" in html
        assert "committer" in html

    def test_member_is_bold_without_star(self) -> None:
        html = reviewer(Reviewer("someone", STANDING_MEMBER))
        assert "someone</strong>" in html and "</strong>*" not in html

    def test_plain(self) -> None:
        html = reviewer(Reviewer("drive-by"))
        assert "<strong" not in html and "drive-by" in html

    def test_escapes_login(self) -> None:
        html = reviewer(Reviewer("<script>", STANDING_COMMITTER))
        assert "<script>" not in html and "&lt;script&gt;" in html

    def test_bare_values(self) -> None:
        assert reviewer(None) == ""
        assert reviewer("") == ""
        assert "<strong" not in reviewer("bob") and "bob" in reviewer("bob")
