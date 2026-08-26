"""Tests for the operator's-own-PR search and the cycle's decision summary.

Two related complaints motivated these: "let's do an explicit query for the
user's PRs in polling and updates", and "I'm not seeing any agent ingestions
having happened". The first is a coverage gap (``involves:`` is one
relevance-ranked page shared with every thread the operator ever commented on);
the second was a logging gap (every gate that declines a review was silent or
DEBUG-only, so a cycle that reviewed nothing looked like a cycle that never ran).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.backends.github import GitHubClient
from franktheunicorn.config.models import OperatorConfig, ProjectConfig
from franktheunicorn.core.models import PullRequest
from franktheunicorn.worker import runner


def _item(owner: str, repo: str, number: int) -> dict[str, Any]:
    return {
        "number": number,
        "pull_request": {"url": f"https://api.github.test/repos/{owner}/{repo}/pulls/{number}"},
        "repository_url": f"https://api.github.test/repos/{owner}/{repo}",
    }


class TestSearchPrsAuthoredBy:
    @pytest.fixture
    def client(self) -> GitHubClient:
        c = GitHubClient(token="t", base_url="https://api.github.test")
        yield c
        c.close()

    def test_queries_by_author(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json={"items": [_item("apache", "spark", 7)]})

        items = client.search_prs_authored_by("holdenk")

        assert [i["number"] for i in items] == [7]
        query = httpx_mock.get_requests()[0].url.params["q"]
        assert "author:holdenk" in query
        assert "type:pr" in query
        assert "state:open" in query

    def test_sorted_by_recent_activity_not_relevance(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        """A truncated result set should be the N most recently active, not
        whatever GitHub's default relevance ranking put first."""
        httpx_mock.add_response(json={"items": []})

        client.search_prs_authored_by("holdenk")

        params = httpx_mock.get_requests()[0].url.params
        assert params["sort"] == "updated"
        assert params["order"] == "desc"

    def test_paginates_past_one_page(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        """per_page was passed as max_results, which GitHub clamps to 100 — so
        a caller asking for more quietly got exactly 100."""
        httpx_mock.add_response(json={"items": [_item("a", "b", n) for n in range(100)]})
        httpx_mock.add_response(json={"items": [_item("a", "b", 100)]})

        items = client.search_prs_authored_by("holdenk", max_results=150)

        assert len(items) == 101
        assert len(httpx_mock.get_requests()) == 2

    def test_never_returns_more_than_asked_for(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        httpx_mock.add_response(json={"items": [_item("a", "b", n) for n in range(100)]})

        items = client.search_prs_authored_by("holdenk", max_results=10)

        assert len(items) == 10

    def test_a_rate_limit_keeps_the_pages_already_fetched(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        """Discarding page one because page two was throttled loses real work."""
        httpx_mock.add_response(json={"items": [_item("a", "b", n) for n in range(100)]})
        httpx_mock.add_response(status_code=403, json={"message": "rate limited"})

        items = client.search_prs_authored_by("holdenk", max_results=200)

        assert len(items) == 100

    def test_a_mid_pagination_exception_keeps_the_first_page(self, client: GitHubClient) -> None:
        """A second-page timeout must not discard the first page's results."""
        page_one = MagicMock(status_code=200)
        page_one.json.return_value = {"items": [_item("a", "b", n) for n in range(100)]}

        with patch.object(client, "_get", side_effect=[page_one, TimeoutError("slow")]):
            items = client.search_prs_authored_by("holdenk", max_results=200)

        assert len(items) == 100

    def test_other_forges_return_nothing_rather_than_erroring(self) -> None:
        """The base class default — Gitea/GitLab have no equivalent search."""
        from franktheunicorn.backends.gitea import GiteaClient

        gitea = GiteaClient(token="t", base_url="https://git.example.test")
        try:
            assert gitea.search_prs_authored_by("holdenk") == []
        finally:
            gitea.close()


@pytest.mark.django_db
class TestScanOperatorPrs:
    """Two searches per forge, deduped, own-PRs first."""

    def _client(self, authored: list[dict[str, Any]], involved: list[dict[str, Any]]) -> MagicMock:
        client = MagicMock()
        client.search_prs_authored_by.return_value = authored
        client.search_prs_involving.return_value = involved
        return client

    def test_both_searches_run(self) -> None:
        client = self._client([_item("apache", "spark", 1)], [_item("apache", "spark", 2)])

        with patch("franktheunicorn.backends.poller.ingest_single_pr") as ingest:
            runner._scan_mentioned_prs({"github": client}, "holdenk", None)

        client.search_prs_authored_by.assert_called_once_with("holdenk")
        client.search_prs_involving.assert_called_once_with("holdenk")
        assert {call.args[2] for call in ingest.call_args_list} == {1, 2}

    def test_own_prs_are_ingested_first(self) -> None:
        """If anything downstream gives up part-way, the operator's own PRs are
        the part that landed."""
        client = self._client([_item("apache", "spark", 9)], [_item("apache", "spark", 3)])

        with patch("franktheunicorn.backends.poller.ingest_single_pr") as ingest:
            runner._scan_mentioned_prs({"github": client}, "holdenk", None)

        assert [call.args[2] for call in ingest.call_args_list] == [9, 3]

    def test_a_pr_in_both_results_is_ingested_once(self) -> None:
        """Each ingest is a detail fetch plus a files fetch."""
        both = [_item("apache", "spark", 42)]
        client = self._client(both, list(both))

        with patch("franktheunicorn.backends.poller.ingest_single_pr") as ingest:
            runner._scan_mentioned_prs({"github": client}, "holdenk", None)

        assert ingest.call_count == 1

    def test_a_forge_without_the_search_is_skipped(self) -> None:
        bare = MagicMock(spec=[])  # no search methods at all

        with patch("franktheunicorn.backends.poller.ingest_single_pr") as ingest:
            runner._scan_mentioned_prs({"gitea": bare}, "holdenk", None)

        assert ingest.call_count == 0

    def test_one_failed_ingest_does_not_stop_the_rest(self, caplog: Any) -> None:
        client = self._client(
            [_item("apache", "spark", 1), _item("apache", "spark", 2)],
            [],
        )

        with (
            patch(
                "franktheunicorn.backends.poller.ingest_single_pr",
                side_effect=[RuntimeError("boom"), None],
            ) as ingest,
            caplog.at_level(logging.INFO),
        ):
            runner._scan_mentioned_prs({"github": client}, "holdenk", None)

        assert ingest.call_count == 2
        # WARNING, not DEBUG: a PR that failed to ingest is a PR the operator
        # will not see in the dashboard this cycle.
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_a_plain_issue_is_not_ingested_as_a_pr(self) -> None:
        issue = {"number": 5, "repository_url": "https://api.github.test/repos/a/b"}
        client = self._client([issue], [])

        with patch("franktheunicorn.backends.poller.ingest_single_pr") as ingest:
            runner._scan_mentioned_prs({"github": client}, "holdenk", None)

        assert ingest.call_count == 0

    def test_a_search_that_raises_does_not_stop_the_other(self) -> None:
        client = MagicMock()
        client.search_prs_authored_by.side_effect = RuntimeError("throttled")
        client.search_prs_involving.return_value = [_item("apache", "spark", 4)]

        with patch("franktheunicorn.backends.poller.ingest_single_pr") as ingest:
            runner._scan_mentioned_prs({"github": client}, "holdenk", None)

        assert [call.args[2] for call in ingest.call_args_list] == [4]


class TestCycleSummary:
    """One line per cycle saying what ran and what stopped the rest."""

    def test_reports_the_counts(self, caplog: Any) -> None:
        with caplog.at_level(logging.INFO):
            runner._log_cycle_summary(412, 8, Counter({"ran": 2, "already-reviewed": 6}))

        assert "412 PR(s) seen" in caplog.text
        assert "8 refreshed" in caplog.text
        assert "2 ran" in caplog.text
        assert "already-reviewed 6" in caplog.text

    def test_nothing_ran_names_the_gate_and_the_knob(self, caplog: Any) -> None:
        """The whole point: "no agent ingestions" must be answerable from the log."""
        with caplog.at_level(logging.INFO):
            runner._log_cycle_summary(30, 30, Counter({"not-involved": 29, "wip": 1}))

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a cycle that reviewed nothing must say so above INFO"
        text = warnings[0].getMessage()
        assert "not-involved" in text
        assert "auto_review_policy" in text, "say which setting changes it"

    def test_a_cycle_that_reviewed_something_does_not_warn(self, caplog: Any) -> None:
        with caplog.at_level(logging.INFO):
            runner._log_cycle_summary(10, 1, Counter({"ran": 1, "not-involved": 9}))

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_an_idle_cycle_does_not_warn(self, caplog: Any) -> None:
        """Nothing to review is not a problem to report."""
        with caplog.at_level(logging.INFO):
            runner._log_cycle_summary(0, 0, Counter())

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.django_db
class TestReviewSkipReason:
    """The gate decisions, now with a reason the operator can act on."""

    def test_force_bypasses_every_gate(self, db_pr: PullRequest) -> None:
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="none")

        assert runner.review_skip_reason(db_pr, pc, None, force=True) is None

    def test_already_reviewed_is_named(self, db_pr: PullRequest) -> None:
        from tests.factories import ReviewDraftFactory

        ReviewDraftFactory(pull_request=db_pr)
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="all")

        skip = runner.review_skip_reason(db_pr, pc, None, force=False)

        assert skip is not None
        assert skip.reason == "already-reviewed"
        assert "Force Run" in skip.explanation

    def test_wip_is_named(self, db_pr: PullRequest) -> None:
        db_pr.queue = "wip"
        db_pr.save()
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="all")

        skip = runner.review_skip_reason(db_pr, pc, None, force=False)

        assert skip is not None
        assert skip.reason == "wip"

    def test_policy_none_names_the_setting(self, db_pr: PullRequest) -> None:
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="none")

        skip = runner.review_skip_reason(db_pr, pc, None, force=False)

        assert skip is not None
        assert skip.reason == "policy-none"
        assert "auto_review_policy" in skip.explanation

    def test_not_involved_names_the_operator_and_the_setting(self, db_pr: PullRequest) -> None:
        # The fixture makes holdenk a requested reviewer, which is exactly what
        # the policy looks for — clear it to reach the not-involved branch.
        db_pr.requested_reviewers = []
        db_pr.save()
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="mentioned_or_authored")
        oc = OperatorConfig(github_username="holdenk")

        skip = runner.review_skip_reason(db_pr, pc, oc, force=False)

        assert skip is not None
        assert skip.reason == "not-involved"
        assert "holdenk" in skip.explanation
        assert "auto_review_policy: all" in skip.explanation

    def test_the_operators_own_pr_passes_the_policy(self, db_pr: PullRequest) -> None:
        db_pr.author = "holdenk"
        db_pr.save()
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="mentioned_or_authored")
        oc = OperatorConfig(github_username="holdenk")

        assert runner.review_skip_reason(db_pr, pc, oc, force=False) is None


class TestBackendPreflightReporting:
    """Every configured backend gets a verdict, and a dead set says so loudly."""

    def test_no_backends_at_all_is_an_error(self, caplog: Any) -> None:
        with caplog.at_level(logging.INFO):
            runner._check_backends(OperatorConfig(llm_backends=[]))

        assert any(r.levelno >= logging.ERROR for r in caplog.records)
        assert "No llm_backends configured" in caplog.text

    def test_a_stub_backend_is_named_not_skipped_silently(self, caplog: Any) -> None:
        """A stub reviewer produces canned text, which is exactly the state where
        an operator thinks reviews work and they don't."""
        from franktheunicorn.config.models import LLMBackendConfig

        oc = OperatorConfig(llm_backends=[LLMBackendConfig(provider="stub", model="none")])

        with caplog.at_level(logging.INFO):
            runner._check_backends(oc)

        assert "stub" in caplog.text
        assert "canned text" in caplog.text

    def test_a_missing_key_is_reported_as_disabled(self, caplog: Any) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        oc = OperatorConfig(
            llm_backends=[
                LLMBackendConfig(provider="claude", model="x", api_key_env="NOT_SET_ANYWHERE_XYZ")
            ]
        )

        with caplog.at_level(logging.INFO):
            disabled = runner._check_backends(oc)

        assert disabled == frozenset({0})
        assert "DISABLED" in caplog.text
        # And the roll-up, so an operator does not have to add the lines up.
        assert "LLM backends:" in caplog.text

    def test_every_backend_failing_is_escalated_to_error(self, caplog: Any) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        oc = OperatorConfig(
            llm_backends=[
                LLMBackendConfig(provider="claude", model="x", api_key_env="NOT_SET_XYZ_1"),
                LLMBackendConfig(provider="openai", model="y", api_key_env="NOT_SET_XYZ_2"),
            ]
        )

        with caplog.at_level(logging.INFO):
            runner._check_backends(oc)

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors
        assert "produce no findings" in errors[0].getMessage()
