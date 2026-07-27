"""Tests for the default review-gating policy in ``process_pr`` (PART B).

The token-saver gate stops the worker from auto-running the expensive LLM
review on every PR. Only PRs the operator is involved in (authored, requested
reviewer, assignee, or @-mentioned in the body) are auto-reviewed under the
default ``mentioned_or_authored`` policy. Ingestion/scoring/routing is NOT
gated — that happens in the poller, upstream of ``process_pr``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import OperatorConfig, ProjectConfig
from franktheunicorn.core.models import ReviewDraft
from franktheunicorn.worker.runner import _should_auto_review, process_pr
from tests.factories import PullRequestFactory


def _tracking_draft_review() -> tuple[dict[str, Any], Any]:
    """A fake ``draft_review`` that records whether it ran."""
    state: dict[str, Any] = {"called": False}

    def fake(pr: Any, pc: Any, **kwargs: Any) -> list[Any]:
        state["called"] = True
        return ["DRAFT"]

    return state, fake


def _op(username: str = "holdenk") -> OperatorConfig:
    # mock_mode bypasses the forge-token validation so we can build a config
    # without a real GITHUB token in the environment.
    return OperatorConfig(github_username=username, mock_mode=True)


def _forge_client() -> MagicMock:
    client = MagicMock()
    client.get_pull_request_diff.return_value = "diff --git a/x b/x"
    return client


@pytest.mark.django_db
class TestShouldAutoReview:
    """Unit tests for the shared gating predicate."""

    def test_operator_authored(self) -> None:
        pr = PullRequestFactory(author="holdenk", requested_reviewers=[], assignees=[], body="")
        assert _should_auto_review(pr, "holdenk") is True

    def test_requested_reviewer(self) -> None:
        pr = PullRequestFactory(author="alice", requested_reviewers=["holdenk"])
        assert _should_auto_review(pr, "holdenk") is True

    def test_assignee(self) -> None:
        pr = PullRequestFactory(author="alice", assignees=["holdenk"])
        assert _should_auto_review(pr, "holdenk") is True

    def test_body_mention(self) -> None:
        pr = PullRequestFactory(author="alice", body="cc @holdenk please review")
        assert _should_auto_review(pr, "holdenk") is True

    def test_uninvolved(self) -> None:
        pr = PullRequestFactory(
            author="alice", requested_reviewers=["bob"], assignees=["carol"], body="nothing here"
        )
        assert _should_auto_review(pr, "holdenk") is False

    def test_empty_operator_reviews_everything(self) -> None:
        pr = PullRequestFactory(author="alice", requested_reviewers=[], assignees=[], body="")
        assert _should_auto_review(pr, "") is True


@pytest.mark.django_db
class TestProcessPrReviewGate:
    def test_default_policy_skips_uninvolved_pr(self) -> None:
        pr = PullRequestFactory(
            state="open", author="alice", requested_reviewers=[], assignees=[], body="routine"
        )
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)  # default policy

        state, fake = _tracking_draft_review()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake):
            result = process_pr(pr, config, operator_config=_op(), forge_client=_forge_client())

        assert result == []
        assert state["called"] is False
        # Still ingested: the PR row is untouched, just not reviewed.
        pr.refresh_from_db()
        assert pr.state == "open"
        assert not ReviewDraft.objects.filter(pull_request=pr).exists()

    def test_default_policy_reviews_authored_pr(self) -> None:
        pr = PullRequestFactory(state="open", author="holdenk")
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)

        state, fake = _tracking_draft_review()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake):
            result = process_pr(pr, config, operator_config=_op(), forge_client=_forge_client())

        assert state["called"] is True
        assert result == ["DRAFT"]

    def test_default_policy_reviews_mentioned_pr(self) -> None:
        pr = PullRequestFactory(state="open", author="alice", requested_reviewers=["holdenk"])
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)

        state, fake = _tracking_draft_review()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake):
            result = process_pr(pr, config, operator_config=_op(), forge_client=_forge_client())

        assert state["called"] is True
        assert result == ["DRAFT"]

    def test_policy_all_reviews_uninvolved_pr(self) -> None:
        pr = PullRequestFactory(
            state="open", author="alice", requested_reviewers=[], assignees=[], body="routine"
        )
        config = ProjectConfig(
            owner=pr.project.owner, repo=pr.project.repo, auto_review_policy="all"
        )

        state, fake = _tracking_draft_review()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake):
            result = process_pr(pr, config, operator_config=_op(), forge_client=_forge_client())

        assert state["called"] is True
        assert result == ["DRAFT"]

    def test_policy_none_skips_even_involved_pr(self) -> None:
        pr = PullRequestFactory(state="open", author="holdenk")  # operator-authored, still skipped
        config = ProjectConfig(
            owner=pr.project.owner, repo=pr.project.repo, auto_review_policy="none"
        )

        state, fake = _tracking_draft_review()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake):
            result = process_pr(pr, config, operator_config=_op(), forge_client=_forge_client())

        assert result == []
        assert state["called"] is False

    def test_force_bypasses_gate(self) -> None:
        pr = PullRequestFactory(
            state="open", author="alice", requested_reviewers=[], assignees=[], body="routine"
        )
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)  # default policy

        state, fake = _tracking_draft_review()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake):
            result = process_pr(
                pr, config, operator_config=_op(), forge_client=_forge_client(), force=True
            )

        assert state["called"] is True
        assert result == ["DRAFT"]

    def test_empty_operator_username_reviews_everything(self) -> None:
        pr = PullRequestFactory(
            state="open", author="alice", requested_reviewers=[], assignees=[], body="routine"
        )
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)  # default policy

        state, fake = _tracking_draft_review()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake):
            result = process_pr(pr, config, operator_config=_op(""), forge_client=_forge_client())

        assert state["called"] is True
        assert result == ["DRAFT"]
