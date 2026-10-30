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

from franktheunicorn.config.models import AgentCLIReviewerConfig, OperatorConfig, ProjectConfig
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


@pytest.mark.django_db
class TestOnlyOwedReviewersRun:
    """A reviewer coming back from a usage-limit pause is owed one turn. The rest
    of the PR's reviewers already had theirs and must not re-run and re-file."""

    def _setup(self) -> tuple[Any, ProjectConfig, OperatorConfig]:
        pr = PullRequestFactory(state="open", author="holdenk", head_sha="abc")
        done = {"status": "ok", "findings": 0, "head_sha": "abc"}
        pr.agent_runs = {"llm": done, "claude": done}
        pr.save()
        oc = _op()
        # Set directly: resolving would seed whichever agent binaries this box has.
        oc._resolved_agent_cli_reviewers = [
            AgentCLIReviewerConfig(name="claude", enabled=True),
            AgentCLIReviewerConfig(name="cursor-agent", enabled=True),
        ]
        return pr, ProjectConfig(owner=pr.project.owner, repo=pr.project.repo), oc

    def _ran(self, *, paused: bool, force: bool = False) -> list[str]:
        pr, pc, oc = self._setup()
        ran: list[str] = []
        with (
            patch("franktheunicorn.review.drafter.draft_review", return_value=[]),
            patch(
                "franktheunicorn.worker.runner._run_agent_cli_for_pr",
                side_effect=lambda _pr, reviewer, *a, **k: ran.append(reviewer.name) or "ok",
            ),
            patch("franktheunicorn.review.agent_cli.usage_paused", return_value=paused),
        ):
            process_pr(pr, pc, operator_config=oc, forge_client=_forge_client(), force=force)
        return ran

    def test_only_the_missing_reviewer_runs(self) -> None:
        assert self._ran(paused=False) == ["cursor-agent"]

    def test_a_paused_reviewer_is_not_owed(self) -> None:
        assert self._ran(paused=True) == []

    def test_force_probes_a_paused_reviewer(self) -> None:
        assert self._ran(paused=True, force=True) == ["claude", "cursor-agent"]

    def test_a_sibling_pausing_mid_loop_stops_the_next(self) -> None:
        pr, pc, oc = self._setup()
        pr.agent_runs = {"llm": pr.agent_runs["llm"]}
        pr.save()
        ran: list[str] = []
        paused = {"now": False}

        def _run(_pr: Any, reviewer: Any, *a: Any, **k: Any) -> str:
            ran.append(reviewer.name)
            paused["now"] = True
            return "paused"

        with (
            patch("franktheunicorn.review.drafter.draft_review", return_value=[]),
            patch("franktheunicorn.worker.runner._run_agent_cli_for_pr", side_effect=_run),
            patch(
                "franktheunicorn.review.agent_cli.usage_paused",
                side_effect=lambda _rc: paused["now"],
            ),
        ):
            process_pr(pr, pc, operator_config=oc, forge_client=_forge_client())
        assert ran == ["claude"]


@pytest.mark.django_db
class TestTriagePolicy:
    """policy: triage — every PR gets the cheap call, only some get the full review."""

    def _run(self, verdict: str, **pr_kw: Any) -> tuple[bool, Any]:
        from franktheunicorn.review.mergeability import Triage

        defaults: dict[str, Any] = {
            "state": "open",
            "author": "alice",
            "requested_reviewers": [],
            "assignees": [],
            "body": "",
            "head_sha": "abc",
        }
        pr = PullRequestFactory(**{**defaults, **pr_kw})
        pc = ProjectConfig(
            owner=pr.project.owner, repo=pr.project.repo, auto_review_policy="triage"
        )
        state, fake = _tracking_draft_review()
        with (
            patch("franktheunicorn.review.drafter.draft_review", side_effect=fake),
            patch(
                "franktheunicorn.review.mergeability.run_triage",
                return_value=Triage(verdict, "why", source="openai/qwen"),
            ) as mock_triage,
        ):
            process_pr(pr, pc, operator_config=_op(), forge_client=_forge_client())
        pr.refresh_from_db()
        assert mock_triage.called
        assert pr.mergeability_verdict == verdict
        return state["called"], pr

    def test_needs_work_gets_no_full_review(self) -> None:
        ran, _ = self._run("needs-work")
        assert ran is False

    def test_likely_mergeable_gets_the_full_review(self) -> None:
        ran, _ = self._run("likely-mergeable")
        assert ran is True

    def test_an_interesting_pr_is_reviewed_anyway(self) -> None:
        ran, _ = self._run("needs-work", author="holdenk")
        assert ran is True

    def test_a_triaged_head_is_skipped_before_any_fetch(self) -> None:
        from franktheunicorn.worker.runner import review_skip_reason

        _, pr = self._run("needs-work")
        pc = ProjectConfig(
            owner=pr.project.owner, repo=pr.project.repo, auto_review_policy="triage"
        )
        skip = review_skip_reason(pr, pc, _op(), force=False)
        assert skip is not None and skip.reason == "triage-not-mergeable"


@pytest.mark.django_db
class TestClosedMidCycle:
    def test_a_pr_closed_since_it_was_read_is_skipped(self) -> None:
        from franktheunicorn.core.models import PullRequest
        from franktheunicorn.worker.runner import review_skip_reason

        pr = PullRequestFactory(state="open", author="holdenk")
        PullRequest.objects.filter(pk=pr.pk).update(state="closed")
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        skip = review_skip_reason(pr, pc, _op(), force=False)
        assert skip is not None and skip.reason == "closed"

    def test_the_reap_is_throttled(self) -> None:
        from franktheunicorn.worker import runner

        pc = ProjectConfig(owner="apache", repo="spark")
        clients = {pc.forge: MagicMock()}
        runner._last_closed_reap = float("-inf")
        with patch(
            "franktheunicorn.backends.poller.reap_closed_pull_requests", return_value=2
        ) as reap:
            runner._reap_closed_prs_if_due(clients, [pc])
            runner._reap_closed_prs_if_due(clients, [pc])
        assert reap.call_count == 1
