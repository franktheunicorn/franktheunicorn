"""Tests for the approved-but-critical flag and its dashboard membership."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import ReviewDraft
from franktheunicorn.review.approved_critical import update_approved_critical
from tests.factories import PullRequestFactory


def _pc(**kw: object) -> ProjectConfig:
    return ProjectConfig(owner="apache", repo="spark", **kw)


def _approve(pr: object, *logins: str) -> None:
    pr.review_states = {login: {"state": "APPROVED"} for login in logins}
    pr.save()


def _critical_draft(pr: object, status: str = "pending") -> ReviewDraft:
    return ReviewDraft.objects.create(
        pull_request=pr,
        comment_body="this can drop the recovery state on failover",
        severity="critical",
        status=status,
    )


@pytest.mark.django_db
class TestUpdateApprovedCritical:
    def test_committer_approval_plus_critical_draft_flags(self) -> None:
        pr = PullRequestFactory(state="open", review_states={"cloud-fan": {"state": "APPROVED"}})
        draft = _critical_draft(pr)

        assert update_approved_critical(pr, _pc(committers=["cloud-fan"])) is True
        pr.refresh_from_db()
        assert pr.approved_critical is True
        assert pr.approved_critical_detail["approvers"] == ["cloud-fan"]
        assert pr.approved_critical_detail["draft_ids"] == [draft.pk]

    def test_non_committer_approval_does_not_flag(self) -> None:
        pr = PullRequestFactory(state="open", review_states={"someone": {"state": "APPROVED"}})
        _critical_draft(pr)
        assert update_approved_critical(pr, _pc(committers=["cloud-fan"])) is False

    def test_important_severity_does_not_flag(self) -> None:
        pr = PullRequestFactory(state="open", review_states={"cloud-fan": {"state": "APPROVED"}})
        ReviewDraft.objects.create(
            pull_request=pr,
            comment_body="a real but ordinary finding",
            severity="important",
        )
        assert update_approved_critical(pr, _pc(committers=["cloud-fan"])) is False

    def test_rejected_critical_draft_does_not_flag(self) -> None:
        pr = PullRequestFactory(state="open", review_states={"cloud-fan": {"state": "APPROVED"}})
        _critical_draft(pr, status="rejected")
        assert update_approved_critical(pr, _pc(committers=["cloud-fan"])) is False

    def test_no_committers_list_clears(self) -> None:
        pr = PullRequestFactory(state="open", approved_critical=True, review_states={})
        assert update_approved_critical(pr, _pc()) is False
        pr.refresh_from_db()
        assert pr.approved_critical is False

    def test_withdrawn_approval_clears(self) -> None:
        pr = PullRequestFactory(
            state="open", review_states={"cloud-fan": {"state": "CHANGES_REQUESTED"}}
        )
        _critical_draft(pr)
        pr.approved_critical = True
        assert update_approved_critical(pr, _pc(committers=["cloud-fan"])) is False

    def test_case_insensitive_committer_match(self) -> None:
        pr = PullRequestFactory(state="open", review_states={"Cloud-Fan": {"state": "APPROVED"}})
        _critical_draft(pr)
        assert update_approved_critical(pr, _pc(committers=["cloud-fan"])) is True


@pytest.mark.django_db
class TestApprovedCriticalTabMembership:
    def test_the_tab_filters_open_flagged_prs(self) -> None:
        from franktheunicorn.core.models import PullRequest
        from franktheunicorn.dashboard.views import (
            APPROVED_CRITICAL_QUEUE,
            _queue_members_q,
        )

        flagged = PullRequestFactory(state="open", approved_critical=True)
        PullRequestFactory(state="open", approved_critical=False)
        PullRequestFactory(state="closed", approved_critical=True)

        members = PullRequest.objects.filter(_queue_members_q(APPROVED_CRITICAL_QUEUE))
        assert list(members) == [flagged]
