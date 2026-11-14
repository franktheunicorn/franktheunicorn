"""Fresh forge-state checks for every normal review path."""

from __future__ import annotations

from collections import Counter
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.backends.base import ForgeClient
from franktheunicorn.config.models import OperatorConfig, ProjectConfig
from franktheunicorn.worker.runner import process_pr, refresh_review_state
from tests.factories import PullRequestFactory


def _operator() -> OperatorConfig:
    """Disable installed agents so only explicitly exercised reviewers run."""
    config = OperatorConfig(mock_mode=True)
    for reviewer in config.agent_cli_reviewers:
        reviewer.enabled = False
    return config


@pytest.mark.django_db
class TestRefreshReviewState:
    @pytest.mark.parametrize("data", [{"state": "open"}, {"state": "open", "merged": False}])
    def test_open_response_permits_review(self, data: dict[str, object]) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        client = MagicMock(spec=ForgeClient)
        client.get_pull_request.return_value = data
        assert refresh_review_state(pr, pc, client) is None
        client.get_pull_request.assert_called_once_with(pc.owner, pc.repo, pr.number)

    @pytest.mark.parametrize(
        "data",
        [
            {"state": "closed"},
            {"state": "merged"},
            {"state": "open", "merged": True},
            {"state": "open", "merged_at": "2026-11-14T00:00:00Z"},
            {"state": "closed", "merged_at": "2026-11-14T00:00:00"},
        ],
    )
    def test_closed_or_merged_response_updates_stale_row(self, data: dict[str, object]) -> None:
        pr = PullRequestFactory(state="open")
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        client = MagicMock(spec=ForgeClient)
        client.get_pull_request.return_value = data
        skip = refresh_review_state(pr, pc, client)
        assert skip is not None and skip.reason == "closed"
        pr.refresh_from_db()
        assert pr.state == "closed"
        if "merged_at" in data:
            assert pr.merged_at is not None

    @pytest.mark.parametrize("data", [{}, {"state": "unknown"}, {"state": []}, None])
    def test_unknown_response_defers_review_without_closing_row(self, data: object) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        client = MagicMock(spec=ForgeClient)
        client.get_pull_request.return_value = data
        skip = refresh_review_state(pr, pc, client)
        assert skip is not None and skip.reason == "state-unavailable"
        pr.refresh_from_db()
        assert pr.state == "open"

    def test_read_failure_defers_review_without_closing_row(self) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        client = MagicMock(spec=ForgeClient)
        client.get_pull_request.side_effect = RuntimeError("rate limited")
        skip = refresh_review_state(pr, pc, client)
        assert skip is not None and "rate limited" in skip.explanation
        pr.refresh_from_db()
        assert pr.state == "open"

    def test_missing_client_defers_review(self) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        skip = refresh_review_state(pr, pc, None)
        assert skip is not None and skip.reason == "state-unavailable"


@pytest.mark.django_db
class TestNormalReviewStateGate:
    @pytest.mark.parametrize("force", [False, True])
    @pytest.mark.parametrize("state", ["closed", "merged"])
    def test_closed_rows_are_skipped_even_when_forced(self, state: str, force: bool) -> None:
        pr = PullRequestFactory(state=state, queue="zombie")
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo, auto_review_policy="all")
        client = MagicMock(spec=ForgeClient)
        with patch("franktheunicorn.review.drafter.draft_review") as review:
            assert process_pr(pr, pc, _operator(), forge_client=client, force=force) == []
        review.assert_not_called()
        client.get_pull_request.assert_not_called()

    @pytest.mark.parametrize("force", [False, True])
    def test_closed_upstream_is_checked_before_diff_or_model(self, force: bool) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo, auto_review_policy="all")
        client = MagicMock(spec=ForgeClient)
        client.get_pull_request.return_value = {"state": "closed", "merged": True}
        decisions: Counter[str] = Counter()
        logs: list[str] = []
        with patch("franktheunicorn.review.drafter.draft_review") as review:
            assert (
                process_pr(
                    pr,
                    pc,
                    _operator(),
                    forge_client=client,
                    force=force,
                    decisions=decisions,
                    log_lines=logs,
                )
                == []
            )
        review.assert_not_called()
        client.get_pull_request_diff.assert_not_called()
        assert decisions["closed"] == 1
        assert "closed or merged" in logs[0]

    def test_unknown_live_state_does_not_run_any_review(self) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        client = MagicMock(spec=ForgeClient)
        client.get_pull_request.return_value = {}
        with patch("franktheunicorn.review.drafter.draft_review") as review:
            assert process_pr(pr, pc, _operator(), forge_client=client, force=True) == []
        review.assert_not_called()
        assert not pr.agent_runs

    def test_closure_while_fetching_context_stops_main_review(self) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        client = MagicMock(spec=ForgeClient)
        client.get_pull_request.side_effect = [{"state": "open"}, {"state": "closed"}]
        client.get_pull_request_diff.return_value = "diff"
        with patch("franktheunicorn.review.drafter.draft_review") as review:
            assert process_pr(pr, pc, _operator(), forge_client=client, force=True) == []
        review.assert_not_called()
        assert client.get_pull_request.call_count == 2

    def test_closure_after_main_review_stops_cli_review(self) -> None:
        pr = PullRequestFactory()
        pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        operator = _operator()
        next(r for r in operator.agent_cli_reviewers if r.name == "codex").enabled = True
        client = MagicMock(spec=ForgeClient)
        client.get_pull_request.side_effect = [
            {"state": "open"},
            {"state": "open"},
            {"state": "closed"},
        ]
        client.get_pull_request_diff.return_value = "diff"
        with (
            patch("franktheunicorn.review.drafter.draft_review", return_value=[]),
            patch("franktheunicorn.worker.runner._run_agent_cli_for_pr") as cli,
        ):
            process_pr(pr, pc, operator, forge_client=client, force=True)
        cli.assert_not_called()
        assert client.get_pull_request.call_count == 3


@pytest.mark.django_db
@pytest.mark.parametrize("merged_at", ["not-a-date", "2026-99-99T01:00:00Z"])
def test_bad_merge_timestamp_still_blocks_review(merged_at: str) -> None:
    pr = PullRequestFactory()
    pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
    client = MagicMock(spec=ForgeClient)
    client.get_pull_request.return_value = {"state": "closed", "merged_at": merged_at}
    skip = refresh_review_state(pr, pc, client)
    assert skip is not None and skip.reason == "closed"
    pr.refresh_from_db()
    assert pr.state == "closed"


@pytest.mark.django_db
def test_stale_object_with_newly_merged_database_row_cannot_be_forced() -> None:
    from django.utils import timezone

    from franktheunicorn.worker.runner import review_skip_reason

    pr = PullRequestFactory(state="open")
    pc = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
    type(pr).objects.filter(pk=pr.pk).update(merged_at=timezone.now())
    skip = review_skip_reason(pr, pc, _operator(), force=True)
    assert skip is not None and skip.reason == "closed"
