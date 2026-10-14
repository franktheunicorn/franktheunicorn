"""Poller wiring for the intending-to-merge queue.

The detector itself is covered in tests/worker/test_merge_intent.py; these
check that the poll and single-PR ingest paths fetch reviews, write the
fields, route to the queue, and fail soft.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.backends.mock import MockGitHubClient
from franktheunicorn.backends.poller import ingest_single_pr, poll_project
from franktheunicorn.config.models import MergeIntentConfig, OperatorConfig, ProjectConfig
from franktheunicorn.core.models import PullRequest
from tests.factories import ProjectFactory, PullRequestFactory


class _ReviewsClient(MockGitHubClient):
    """MockGitHubClient with scripted reviews, comments and CI per PR."""

    def __init__(self, fixtures_dir: str | Path) -> None:
        super().__init__(fixtures_dir)
        self.reviews: dict[int, list[dict[str, Any]]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.ci: dict[str, str] = {}
        self.review_calls: list[int] = []
        self.ci_calls: list[str] = []
        self.reviews_raise = False

    def list_pull_request_reviews(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        self.review_calls.append(pr_number)
        if self.reviews_raise:
            raise RuntimeError("503")
        return self.reviews.get(pr_number, [])

    def get_issue_comments(
        self, owner: str, repo: str, issue_number: int, since: str | None = None
    ) -> list[dict[str, Any]]:
        return self.comments.get(issue_number, [])

    def get_commit_ci_status(self, owner: str, repo: str, sha: str) -> str:
        self.ci_calls.append(sha)
        return self.ci.get(sha, "")


def _approval(login: str = "holdenk", body: str = "", at: str = "2026-10-01T10:00:00Z") -> dict:
    return {
        "user": {"login": login},
        "state": "APPROVED",
        "body": body,
        "submitted_at": at,
        "commit_id": "1" * 40,
    }


def _comment(login: str, body: str, at: str = "2026-10-01T10:00:00Z") -> dict[str, Any]:
    return {"user": {"login": login}, "body": body, "created_at": at}


@pytest.mark.django_db
class TestPollMergeIntent:
    def _config(self) -> ProjectConfig:
        return ProjectConfig(owner="apache", repo="spark")

    def test_operator_approval_routes_to_queue(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]
        client.ci["1" * 40] = "pass"

        poll_project(client, self._config(), operator_username="holdenk")

        pr = PullRequest.objects.get(number=42)
        assert pr.merge_intent == "approved"
        assert pr.merge_intent_by == "holdenk"
        assert pr.merge_intent_source == "review"
        assert pr.merge_intent_commit_sha == "1" * 40
        assert pr.queue == "intending-to-merge"
        assert pr.approval_count == 1
        # CI is read for a queued PR, against its head.
        assert client.ci_calls == ["1" * 40]
        assert pr.ci_status == "pass"
        # The others got their reviews read too (one call each) but no CI read.
        assert sorted(client.review_calls) == [42, 43, 44]

    def test_conditional_comment_routes_to_queue(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.comments[43] = [_comment("holdenk", "LGTM pending CI, will merge after.")]

        poll_project(client, self._config(), operator_username="holdenk")

        pr = PullRequest.objects.get(number=43)
        assert pr.merge_intent == "conditional"
        assert pr.merge_intent_source == "comment"
        assert pr.queue == "intending-to-merge"
        assert "LGTM pending CI" in pr.merge_intent_excerpt
        # A comment carries no commit id: baseline on the head seen now.
        assert pr.merge_intent_commit_sha == "1" * 40

    def test_someone_elses_approval_does_not_count(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval("cloud-fan")]

        poll_project(client, self._config(), operator_username="holdenk")

        pr = PullRequest.objects.get(number=42)
        assert pr.merge_intent == ""
        assert pr.queue != "intending-to-merge"
        # ...but it is still an approval as far as the merge queue is concerned.
        assert pr.approval_count == 1

    def test_configured_approvers_override_operator(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval("cloud-fan")]
        cfg = MergeIntentConfig(approvers=["@cloud-fan"])

        poll_project(client, self._config(), operator_username="holdenk", merge_intent=cfg)

        pr = PullRequest.objects.get(number=42)
        assert pr.merge_intent == "approved"
        assert pr.merge_intent_by == "cloud-fan"

    def test_changes_requested_clears_and_reroutes(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]
        poll_project(client, self._config(), operator_username="holdenk")
        assert PullRequest.objects.get(number=42).queue == "intending-to-merge"

        client.reviews[42].append(
            {
                "user": {"login": "holdenk"},
                "state": "CHANGES_REQUESTED",
                "body": "Actually, hold on.",
                "submitted_at": "2026-10-02T10:00:00Z",
                "commit_id": "",
            }
        )
        # A new review bumps the listing's updated_at upstream; force the
        # refresh the same way.
        PullRequest.objects.filter(number=42).update(last_polled_at=None)
        poll_project(client, self._config(), operator_username="holdenk")

        pr = PullRequest.objects.get(number=42)
        assert pr.merge_intent == ""
        assert pr.merge_intent_commit_sha == ""
        # Back to wherever its flags put it — #42 has holdenk as a requested reviewer.
        assert pr.queue == "mentioned"

    def test_failed_reviews_fetch_keeps_stored_intent(self, tmp_path: Path) -> None:
        """A rate-limit window must not empty the queue."""
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]
        poll_project(client, self._config(), operator_username="holdenk")

        client.reviews_raise = True
        PullRequest.objects.filter(number=42).update(last_polled_at=None)
        poll_project(client, self._config(), operator_username="holdenk")

        pr = PullRequest.objects.get(number=42)
        assert pr.merge_intent == "approved"
        assert pr.queue == "intending-to-merge"

    def test_new_head_clears_ci_even_when_the_read_fails(self, tmp_path: Path) -> None:
        """A stale "pass" from the old head must not survive a push."""
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]
        client.ci["1" * 40] = "pass"
        poll_project(client, self._config(), operator_username="holdenk")
        assert PullRequest.objects.get(number=42).ci_status == "pass"

        detail = client.get_pull_request("apache", "spark", 42)
        detail["head"]["sha"] = "2" * 40
        PullRequest.objects.filter(number=42).update(last_polled_at=None)
        with (
            patch.object(client, "get_pull_request", return_value=detail),
            patch.object(client, "get_commit_ci_status", side_effect=RuntimeError("503")),
        ):
            poll_project(client, self._config(), operator_username="holdenk")

        pr = PullRequest.objects.get(number=42)
        assert pr.head_sha == "2" * 40
        assert pr.ci_status == ""
        assert pr.moved_since_approval is True

    def test_disabling_clears_stored_intent_and_reroutes(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]
        poll_project(client, self._config(), operator_username="holdenk")
        assert PullRequest.objects.get(number=42).queue == "intending-to-merge"

        PullRequest.objects.filter(number=42).update(last_polled_at=None)
        poll_project(
            client,
            self._config(),
            operator_username="holdenk",
            merge_intent=MergeIntentConfig(enabled=False),
        )

        pr = PullRequest.objects.get(number=42)
        assert pr.merge_intent == ""
        assert pr.queue != "intending-to-merge"

    def test_disabled_config_reads_nothing(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]

        poll_project(
            client,
            self._config(),
            operator_username="holdenk",
            merge_intent=MergeIntentConfig(enabled=False),
        )

        assert client.review_calls == []
        assert PullRequest.objects.get(number=42).merge_intent == ""

    def test_no_operator_and_no_approvers_reads_nothing(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        poll_project(client, self._config(), operator_username="")
        assert client.review_calls == []

    def test_track_ci_off_skips_ci_read(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]
        poll_project(
            client,
            self._config(),
            operator_username="holdenk",
            merge_intent=MergeIntentConfig(track_ci=False),
        )
        assert client.ci_calls == []
        assert PullRequest.objects.get(number=42).ci_status == ""

    def test_operators_own_pr_gets_ci_without_intent(self, tmp_path: Path) -> None:
        """The merge queue reads ci_status off the operator's own PRs."""
        client = _ReviewsClient(tmp_path)
        client.ci["1" * 40] = "fail"
        # PR #42's demo author is alice-dev.
        poll_project(client, self._config(), operator_username="alice-dev")

        pr = PullRequest.objects.get(number=42)
        assert pr.is_operator_pr is True
        assert pr.ci_status == "fail"

    def test_own_pr_with_merge_comment_outranks_your_prs(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.comments[42] = [_comment("alice-dev", "Merging once CI is green.")]
        poll_project(client, self._config(), operator_username="alice-dev")

        pr = PullRequest.objects.get(number=42)
        assert pr.is_operator_pr is True
        assert pr.merge_intent == "conditional"
        assert pr.queue == "intending-to-merge"

    def test_wip_still_wins_over_intent(self, tmp_path: Path) -> None:
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]
        cfg = ProjectConfig(owner="apache", repo="spark", skip_wip=True)
        with patch.object(
            client,
            "list_pull_requests",
            return_value=[{**p, "draft": True} for p in client.list_pull_requests("a", "s")],
        ):
            poll_project(client, cfg, operator_username="holdenk")
        pr = PullRequest.objects.get(number=42)
        assert pr.merge_intent == "approved"
        assert pr.queue == "wip"


@pytest.mark.django_db
class TestIngestSinglePRMergeIntent:
    def _run(self, client: _ReviewsClient, operator: OperatorConfig) -> PullRequest:
        pc = ProjectConfig(owner="apache", repo="spark")
        with (
            patch("franktheunicorn.config.loader.get_operator_config", return_value=operator),
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch("franktheunicorn.config.resolver.get_forge_entry", return_value=object()),
            patch("franktheunicorn.backends.make_client", return_value=client),
        ):
            return ingest_single_pr("apache", "spark", 42)

    def test_single_ingest_syncs_intent_refs_and_ci(self, tmp_path: Path) -> None:
        ProjectFactory(owner="apache", repo="spark")
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]
        client.ci["1" * 40] = "pending"

        pr = self._run(client, OperatorConfig(github_username="holdenk"))

        assert pr.merge_intent == "approved"
        assert pr.queue == "intending-to-merge"
        # The detail payload's refs are kept, which is what made the CI read possible.
        assert pr.head_sha == "1" * 40
        assert pr.base_sha == "0" * 40
        assert pr.head_branch == "pr-42"
        assert pr.base_branch == "main"
        assert pr.mergeable is True
        assert pr.ci_status == "pending"
        fresh = PullRequest.objects.get(pk=pr.pk)
        assert fresh.merge_intent == "approved"
        assert fresh.ci_status == "pending"

    def test_single_ingest_scores_with_the_comments(self, tmp_path: Path) -> None:
        """The own-PR scan lands here every cycle. Scoring without comments wrote
        over the poll's "ball in our court" bump on exactly those PRs."""
        ProjectFactory(owner="apache", repo="spark")
        client = _ReviewsClient(tmp_path)
        author = self._run(client, OperatorConfig(github_username="holdenk")).author
        client.comments[42] = [_comment(author, "@holdenk could you take another look?")]

        pr = self._run(client, OperatorConfig(github_username="holdenk"))

        assert pr.score_breakdown.get("author_awaiting_maintainer") == 20

    def test_single_ingest_respects_disabled(self, tmp_path: Path) -> None:
        ProjectFactory(owner="apache", repo="spark")
        client = _ReviewsClient(tmp_path)
        client.reviews[42] = [_approval()]
        operator = OperatorConfig(
            github_username="holdenk", merge_intent=MergeIntentConfig(enabled=False)
        )

        pr = self._run(client, operator)

        assert client.review_calls == []
        assert pr.merge_intent == ""

    def test_existing_row_keeps_intent_when_comments_fail(self, tmp_path: Path) -> None:
        """Reviews read fine, comments did not: a comment-based approval stays."""
        project = ProjectFactory(owner="apache", repo="spark")
        PullRequestFactory(
            project=project,
            number=42,
            github_id=1001,
            merge_intent="conditional",
            merge_intent_by="holdenk",
            merge_intent_source="comment",
            queue="intending-to-merge",
        )
        client = _ReviewsClient(tmp_path)
        with patch.object(client, "get_issue_comments", side_effect=RuntimeError("503")):
            pr = self._run(client, OperatorConfig(github_username="holdenk"))

        assert pr.merge_intent == "conditional"
        assert pr.queue == "intending-to-merge"

    def test_existing_row_keeps_intent_when_reviews_fail(self, tmp_path: Path) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        PullRequestFactory(
            project=project,
            number=42,
            github_id=1001,
            merge_intent="approved",
            merge_intent_by="holdenk",
            queue="intending-to-merge",
        )
        client = _ReviewsClient(tmp_path)
        client.reviews_raise = True

        pr = self._run(client, OperatorConfig(github_username="holdenk"))

        assert pr.merge_intent == "approved"
        assert pr.queue == "intending-to-merge"
