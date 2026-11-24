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
import time
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


def _plan_everything(client: Any, pc: ProjectConfig, items: list[dict[str, Any]]) -> Any:
    """Every hit needs a refresh — the scan's routing is under test, not the skip."""
    from franktheunicorn.backends.poller import SearchHitPlan
    from franktheunicorn.core.models import Project

    project, _ = Project.objects.get_or_create(owner=pc.owner, repo=pc.repo)
    return SearchHitPlan(project, [int(i["number"]) for i in items], [])


def _refreshed(_client: Any, _project: Any, _pc: Any, number: int, *a: Any, **k: Any) -> Any:
    return MagicMock(pk=number, number=number, state="open", is_operator_pr=False)


@pytest.mark.django_db
class TestScanOperatorPrs:
    """Three searches per forge, deduped, own-PRs first, configured projects only."""

    _CONFIGS = [ProjectConfig(owner="apache", repo="spark")]

    def _scan(
        self, clients: dict[str, Any], username: str = "holdenk", **kwargs: Any
    ) -> list[runner.OperatorScanHit]:
        return runner._scan_operator_prs(
            clients, username, kwargs.pop("operator_config", None), self._CONFIGS, **kwargs
        )

    def _client(
        self,
        authored: list[dict[str, Any]],
        involved: list[dict[str, Any]],
        reviewed: list[dict[str, Any]] | None = None,
    ) -> MagicMock:
        client = MagicMock()
        client.search_prs_authored_by.return_value = authored
        client.search_prs_reviewed_by.return_value = reviewed or []
        client.search_prs_involving.return_value = involved
        return client

    @staticmethod
    def _patched(refresh: Any = _refreshed) -> Any:
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(
            patch("franktheunicorn.backends.poller.plan_search_hit_refresh", _plan_everything)
        )
        mock = stack.enter_context(
            patch(
                "franktheunicorn.backends.poller.refresh_pull_request_by_number",
                side_effect=refresh,
            )
        )
        return stack, mock

    def _run(self, clients: dict[str, Any], **kwargs: Any) -> tuple[Any, list[Any]]:
        stack, refresh = self._patched(kwargs.pop("refresh", _refreshed))
        with stack:
            hits = self._scan(clients, **kwargs)
        return refresh, hits

    @staticmethod
    def _numbers(refresh: Any) -> list[int]:
        return [call.args[3] for call in refresh.call_args_list]

    def test_all_three_searches_run(self) -> None:
        client = self._client(
            [_item("apache", "spark", 1)],
            [_item("apache", "spark", 2)],
            reviewed=[_item("apache", "spark", 3)],
        )

        refresh, hits = self._run({"github": client})

        client.search_prs_authored_by.assert_called_once_with("holdenk")
        client.search_prs_reviewed_by.assert_called_once_with("holdenk")
        client.search_prs_involving.assert_called_once_with("holdenk")
        assert set(self._numbers(refresh)) == {1, 2, 3}
        assert all(hit.refreshed for hit in hits)

    def test_the_searches_run_in_cost_of_missing_order(self) -> None:
        """A failure part-way through leaves the earlier sets landed, so: own PRs,
        then ones already reviewed, then the wide sweep."""
        client = self._client(
            [_item("apache", "spark", 9)],
            [_item("apache", "spark", 3)],
            reviewed=[_item("apache", "spark", 5)],
        )

        refresh, hits = self._run({"github": client})

        assert self._numbers(refresh) == [9, 5, 3]
        assert [hit.label for hit in hits] == ["authored", "reviewed", "involved"]

    def test_a_pr_in_several_results_is_refreshed_once(self) -> None:
        both = [_item("apache", "spark", 42)]
        client = self._client(both, list(both), reviewed=list(both))

        refresh, hits = self._run({"github": client})

        assert refresh.call_count == 1
        assert [hit.label for hit in hits] == ["authored"], "labelled by its first search"

    def test_a_forge_without_the_search_is_skipped(self) -> None:
        bare = MagicMock(spec=[])  # no search methods at all

        refresh, hits = self._run({"github": bare})

        assert refresh.call_count == 0
        assert hits == []

    def test_one_failed_refresh_does_not_stop_the_rest(self, caplog: Any) -> None:
        client = self._client([_item("apache", "spark", 1), _item("apache", "spark", 2)], [])

        def boom_on_one(*args: Any, **kwargs: Any) -> Any:
            if args[3] == 1:
                msg = "boom"
                raise RuntimeError(msg)
            return _refreshed(*args, **kwargs)

        with caplog.at_level(logging.INFO):
            refresh, hits = self._run({"github": client}, refresh=boom_on_one)

        assert refresh.call_count == 2
        assert [hit.pr.number for hit in hits] == [2]
        # WARNING, not DEBUG: a PR that failed to refresh is stale in the dashboard.
        assert any(r.levelno >= logging.WARNING for r in caplog.records)
        assert "1 failed" in caplog.text

    def test_a_plain_issue_is_not_refreshed_as_a_pr(self) -> None:
        issue = {"number": 5, "repository_url": "https://api.github.test/repos/a/b"}
        client = self._client([issue], [])

        refresh, _ = self._run({"github": client})

        assert refresh.call_count == 0

    def test_a_search_that_raises_warns_and_does_not_stop_the_others(self, caplog: Any) -> None:
        client = MagicMock()
        client.search_prs_authored_by.side_effect = RuntimeError("throttled")
        client.search_prs_reviewed_by.return_value = []
        client.search_prs_involving.return_value = [_item("apache", "spark", 4)]

        with caplog.at_level(logging.INFO):
            refresh, _ = self._run({"github": client})

        assert self._numbers(refresh) == [4]
        # A failed search is a set of PRs that silently stops refreshing.
        assert any(
            r.levelno == logging.WARNING and "Authored PR search failed" in r.getMessage()
            for r in caplog.records
        )

    def test_a_hit_in_an_unconfigured_repo_is_not_refreshed(self) -> None:
        """GitHub's search is account-wide; YAML is the allow-list."""
        client = self._client(
            [_item("holdenk", "some-fork", 9), _item("apache", "spark", 1)],
            [_item("random-org", "drive-by", 3)],
        )

        refresh, _ = self._run({"github": client})

        assert self._numbers(refresh) == [1]

    def test_yaml_owner_repo_match_is_case_insensitive(self) -> None:
        client = self._client([_item("Apache", "Spark", 1)], [])

        refresh, hits = self._run({"github": client})

        assert self._numbers(refresh) == [1]
        assert hits[0].pc.full_name == "apache/spark", "the config's casing, not the hit's"

    def test_a_hit_on_another_forge_than_the_project_is_skipped(self) -> None:
        """A same-named repo on a different forge is not the configured project."""
        client = self._client([_item("apache", "spark", 1)], [])

        refresh, _ = self._run({"gitea": client})

        assert refresh.call_count == 0

    def test_refreshes_run_concurrently_at_refresh_concurrency(self) -> None:
        import threading

        client = self._client([_item("apache", "spark", n) for n in range(1, 9)], [])
        barrier = threading.Barrier(4, timeout=5)

        def wait_for_three_others(*args: Any, **kwargs: Any) -> Any:
            if args[3] <= 4:
                barrier.wait()  # times out unless four refreshes run at once
            return _refreshed(*args, **kwargs)

        oc = OperatorConfig(github_username="holdenk", worker_pool={"refresh_concurrency": 4})
        _, hits = self._run({"github": client}, refresh=wait_for_three_others, operator_config=oc)

        assert [hit.pr.number for hit in hits] == list(range(1, 9)), "still in search order"

    def test_end_to_end_an_unchanged_pr_is_not_fetched_twice(self, tmp_path: Any) -> None:
        """The real planner and refresh: the second scan finds nothing moved."""
        from tests.data_access.github.test_poller_search_refresh import _DetailClient, _search_hit

        client = _DetailClient(tmp_path, [])
        client.search_prs_authored_by = lambda _u: [_search_hit()]  # type: ignore[method-assign]
        configs = [ProjectConfig(owner="apache", repo="spark", poll_refresh_hours=24)]

        first = runner._scan_operator_prs({"github": client}, "holdenk", None, configs)
        PullRequest.objects.filter(number=99).update(score_breakdown={"stub": 1})
        second = runner._scan_operator_prs({"github": client}, "holdenk", None, configs)

        assert client.detail_calls == [99]
        assert [hit.refreshed for hit in first] == [True]
        assert [(hit.pr.number, hit.refreshed) for hit in second] == [(99, False)]


@pytest.mark.django_db
class TestOperatorScanInTheCycle:
    """It runs first, queues what it refreshed, and comes back mid-cycle when due."""

    @staticmethod
    def _hit(number: int, *, own: bool, refreshed: bool = True, state: str = "open") -> Any:
        pr = MagicMock(pk=number, number=number, state=state, is_operator_pr=own)
        return runner.OperatorScanHit(
            pr, ProjectConfig(owner="apache", repo="spark"), MagicMock(), "authored", refreshed
        )

    def _cycle(self, hits: list[Any], **pool: Any) -> tuple[MagicMock, list[tuple[Any, bool]]]:
        from franktheunicorn.worker.review_pool import ReviewPool

        submitted: list[tuple[Any, bool]] = []

        def record(_self: Any, item: Any, *, front: bool = False) -> None:
            submitted.append((item, front))

        oc = OperatorConfig(github_username="holdenk", worker_pool=pool)
        with (
            patch.object(runner, "_scan_operator_prs", return_value=hits) as scan,
            patch.object(ReviewPool, "submit", record),
            patch.object(runner, "_run_shepherding_pass"),
            patch.object(runner, "_fetch_dependency_changelogs_for_cycle"),
            patch.object(runner, "_backfill_unreviewed_prs"),
            patch.object(runner, "_drain_worker_commands"),
        ):
            runner._run_cycle({}, [], "holdenk", oc)
        return scan, submitted

    def test_your_refreshed_pr_goes_to_the_front_and_the_rest_to_the_back(self) -> None:
        scan, submitted = self._cycle(
            [
                self._hit(1, own=True),
                self._hit(2, own=False),
                self._hit(3, own=True, refreshed=False),  # unchanged: nothing to review
                self._hit(4, own=True, state="closed"),  # closed since the search
            ]
        )

        assert scan.call_count == 1, "once at the start; not due again this soon"
        assert [(item.pr.number, front) for item, front in submitted] == [(1, True), (2, False)]

    def test_when_due_it_scans_again_mid_cycle_without_double_queueing(self) -> None:
        scan, submitted = self._cycle([self._hit(1, own=True)], operator_scan_minutes=0)

        assert scan.call_count >= 2, "the main thread's turns re-scan when due"
        assert [item.pr.number for item, _ in submitted] == [1], "one PR, one review"

    def test_the_poll_does_not_requeue_what_the_scan_queued(self) -> None:
        """The scan runs first now; a PR it queued that the poll also refreshes
        must not get a second runner, and the later passes see it once."""
        from franktheunicorn.worker.review_pool import ReviewPool

        own = self._hit(1, own=True)
        polled_copy = MagicMock(pk=1, number=1, state="open", is_operator_pr=True)
        submitted: list[Any] = []
        oc = OperatorConfig(github_username="holdenk")
        pc = ProjectConfig(owner="apache", repo="spark")

        def record(_self: Any, item: Any, *, front: bool = False) -> None:
            submitted.append(item)

        with (
            patch.object(runner, "_scan_operator_prs", return_value=[own]),
            patch.object(ReviewPool, "submit", record),
            patch("franktheunicorn.backends.poller.poll_project", return_value=[polled_copy]),
            patch("franktheunicorn.worker.repo_manager.ensure_repo", return_value=None),
            patch.object(runner, "_maybe_refresh_repo_health"),
            patch.object(runner, "_run_shepherding_pass") as shepherd,
            patch.object(runner, "_fetch_dependency_changelogs_for_cycle"),
            patch.object(runner, "_backfill_unreviewed_prs"),
            patch.object(runner, "_drain_worker_commands"),
        ):
            runner._run_cycle({"github": MagicMock()}, [pc], "holdenk", oc)

        assert [item.pr.number for item in submitted] == [1]
        assert shepherd.call_args.args[0] == [polled_copy], "once, the poll's fresher copy"

    def test_without_a_username_nothing_is_scanned(self) -> None:
        with patch.object(runner, "_scan_operator_prs") as scan:
            runner._run_cycle({}, [], "", OperatorConfig())

        scan.assert_not_called()


class TestSearchUsersDue:
    @pytest.fixture(autouse=True)
    def _clock(self) -> Any:
        runner._operator_scan_clock.reset()
        yield
        runner._operator_scan_clock.reset()

    def test_claims_the_slot_then_waits_out_the_interval(self) -> None:
        oc = OperatorConfig(worker_pool={"operator_scan_minutes": 10})
        assert runner._search_users_due(oc, "holdenk", force=False) == ["holdenk"]
        assert runner._search_users_due(oc, "holdenk", force=False) == []

    def test_force_is_every_user_even_inside_the_interval(self) -> None:
        oc = OperatorConfig(watch={"users": [{}, {"user": "cloud-fan", "source": "search"}]})
        runner._search_users_due(oc, "holdenk", force=False)
        assert runner._search_users_due(oc, "holdenk", force=True) == ["holdenk", "cloud-fan"]

    def test_each_user_has_its_own_clock(self) -> None:
        oc = OperatorConfig(watch={"users": [{}, {"user": "cloud-fan"}]})
        runner._operator_scan_clock.last["holdenk"] = time.monotonic()
        assert runner._search_users_due(oc, "holdenk", force=False) == ["cloud-fan"]

    def test_a_user_read_through_notifications_is_searched_on_the_backstop(self) -> None:
        """Ten minutes is the search users' interval; a notifications user waits
        out search_backstop_minutes instead."""
        oc = OperatorConfig(
            worker_pool={"operator_scan_minutes": 10},
            watch={"search_backstop_minutes": 60, "users": [{}, {"user": "cloud-fan"}]},
        )
        eleven_minutes_ago = time.monotonic() - 11 * 60
        runner._operator_scan_clock.last.update(
            {"holdenk": eleven_minutes_ago, "cloud-fan": eleven_minutes_ago}
        )
        watcher = MagicMock()
        watcher.reads_notifications.side_effect = lambda forge, user: user == "holdenk"
        with patch.object(runner, "_watcher", watcher):
            assert runner._search_users_due(oc, "holdenk", force=False) == ["cloud-fan"]

    def test_no_username_and_no_users_is_nobody(self) -> None:
        assert runner._search_users_due(OperatorConfig(), "", force=True) == []


@pytest.mark.django_db
class TestScanSearchesEachUser:
    def test_every_due_user_is_searched_and_hits_are_deduplicated(self) -> None:
        client = MagicMock(
            spec=["search_prs_authored_by", "search_prs_reviewed_by", "search_prs_involving"]
        )
        client.search_prs_authored_by.return_value = [_item("apache", "spark", 1)]
        client.search_prs_reviewed_by.return_value = []
        client.search_prs_involving.return_value = [_item("apache", "spark", 1)]

        with (
            patch(
                "franktheunicorn.backends.poller.plan_search_hit_refresh",
                side_effect=_plan_everything,
            ),
            patch(
                "franktheunicorn.backends.poller.refresh_pull_request_by_number",
                side_effect=lambda *a, **k: MagicMock(number=a[3], state="open"),
            ),
        ):
            hits = runner._scan_operator_prs(
                {"github": client},
                "holdenk",
                OperatorConfig(github_username="holdenk"),
                [ProjectConfig(owner="apache", repo="spark")],
                users=["holdenk", "cloud-fan"],
            )

        searched = [c.args[0] for c in client.search_prs_authored_by.call_args_list]
        assert searched == ["holdenk", "cloud-fan"]
        assert [h.pr.number for h in hits] == [1], "one PR, however many searches found it"


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

    def test_a_push_rereviews_a_recent_pr(self, db_pr: PullRequest) -> None:
        from django.utils import timezone

        from tests.factories import ReviewDraftFactory

        ReviewDraftFactory(pull_request=db_pr)
        db_pr.head_sha = "b" * 40
        db_pr.github_updated_at = timezone.now()
        db_pr.agent_runs = {
            "llm": {"status": "ok", "findings": 1, "head_sha": "a" * 40},
        }
        db_pr.save()
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="all")

        assert runner.review_skip_reason(db_pr, pc, None, force=False) is None

    def test_a_quiet_pr_keeps_its_review_after_an_old_push(self, db_pr: PullRequest) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from tests.factories import ReviewDraftFactory

        ReviewDraftFactory(pull_request=db_pr)
        db_pr.head_sha = "b" * 40
        db_pr.github_updated_at = timezone.now() - timedelta(days=90)
        db_pr.agent_runs = {
            "llm": {"status": "ok", "findings": 1, "stale": True, "head_sha": "a" * 40},
        }
        db_pr.save()
        pc = ProjectConfig(
            owner="apache", repo="spark", auto_review_policy="all", review_activity_days=30
        )

        skip = runner.review_skip_reason(db_pr, pc, None, force=False)

        assert skip is not None
        assert skip.reason == "already-reviewed"

    def test_a_run_records_the_head_it_reviewed(self, db_pr: PullRequest) -> None:
        db_pr.head_sha = "e" * 40
        db_pr.save()

        runner.record_agent_run(db_pr, "llm", status="ok", findings=2)

        db_pr.refresh_from_db()
        assert db_pr.agent_runs["llm"]["head_sha"] == "e" * 40
        assert "stale" not in db_pr.agent_runs["llm"]

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


@pytest.mark.django_db
class TestRescanningPrsNoAgentCovered:
    """ "Make sure the worker rescans the PRs where none of the agents ran."

    It couldn't, and the reason was that "an agent ran" was inferred from drafts
    existing. A reviewer that ran and found nothing leaves exactly what a reviewer
    that never ran leaves, so the backfill's ``draft_count=0`` filter was wrong in
    both directions at once.
    """

    def _oc(self, *extra: str) -> OperatorConfig:
        """One enabled agent CLI, plus any named in *extra*.

        codex and pi are pinned off explicitly: OperatorConfig seeds all three
        built-ins, and whether the seeded ones resolve otherwise depends on
        what happens to be on this machine's PATH.
        """
        from franktheunicorn.config.models import (
            AgentCLIReviewerConfig,
            _default_agent_cli_reviewers,
        )

        enabled = {"claude", *extra}
        # Names come from the seed list, so adding a built-in reviewer cannot
        # silently widen expected_review_sources underneath these assertions.
        return OperatorConfig(
            github_username="holdenk",
            agent_cli_reviewers=[
                AgentCLIReviewerConfig(name=seed.name, enabled=seed.name in enabled)
                for seed in _default_agent_cli_reviewers()
            ],
        )

    def test_a_reviewer_that_never_ran_is_still_missing(self, db_pr: PullRequest) -> None:
        """The case the whole thing is for: the LLM produced findings, the agent
        CLI failed, and the PR looked fully reviewed."""
        from tests.factories import ReviewDraftFactory

        ReviewDraftFactory(pull_request=db_pr)
        runner.record_agent_run(db_pr, runner.LLM_REVIEW_SOURCE, status="ok", findings=1)

        assert runner.missing_review_sources(db_pr, self._oc()) == {"claude"}

    def test_a_pr_missing_a_reviewer_is_not_skipped_despite_its_drafts(
        self, db_pr: PullRequest
    ) -> None:
        from tests.factories import ReviewDraftFactory

        ReviewDraftFactory(pull_request=db_pr)
        runner.record_agent_run(db_pr, runner.LLM_REVIEW_SOURCE, status="ok", findings=1)
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="all")

        assert runner.review_skip_reason(db_pr, pc, self._oc(), force=False) is None

    def test_a_failed_run_counts_as_having_had_a_turn(self, db_pr: PullRequest) -> None:
        """Otherwise a reviewer that errors on this PR is retried every cycle
        forever — the same runaway the clean-review case had."""
        for source in ("llm", "claude"):
            runner.record_agent_run(db_pr, source, status="failed")

        assert runner.missing_review_sources(db_pr, self._oc()) == set()

    def test_a_clean_review_is_not_rescanned_every_cycle(self, db_pr: PullRequest) -> None:
        """Zero findings and zero drafts used to read as "never reviewed", so the
        backfill re-reviewed a clean PR on every single cycle at full LLM cost."""
        for source in ("llm", "claude"):
            runner.record_agent_run(db_pr, source, status="ok", findings=0)
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="all")

        skip = runner.review_skip_reason(db_pr, pc, self._oc(), force=False)

        assert skip is not None
        assert skip.reason == "reviewed-clean"

    def test_enabling_a_new_reviewer_makes_old_prs_eligible(self, db_pr: PullRequest) -> None:
        """Expected sources come from config, not from what is on the PR."""
        from franktheunicorn.config.models import AgentCLIReviewerConfig

        for source in ("llm", "claude"):
            runner.record_agent_run(db_pr, source, status="ok", findings=0)
        del AgentCLIReviewerConfig  # the helper builds the config

        assert "codex" in runner.missing_review_sources(db_pr, self._oc("codex"))

    def test_a_legacy_pr_with_drafts_is_left_alone(self, db_pr: PullRequest) -> None:
        """Deploying this must not re-review the whole existing database once, at
        LLM cost, to learn what the operator already knows."""
        from tests.factories import ReviewDraftFactory

        ReviewDraftFactory(pull_request=db_pr)

        assert db_pr.agent_runs == {}
        assert runner.missing_review_sources(db_pr, self._oc()) == set()

    def test_a_legacy_pr_with_no_drafts_is_rescanned(self, db_pr: PullRequest) -> None:
        """No drafts and no record is genuinely unreviewed."""
        assert runner.missing_review_sources(db_pr, self._oc()) == {"llm", "claude"}

    def test_the_record_survives_a_concurrent_save(self, db_pr: PullRequest) -> None:
        """The poll cycle writes to this row too; a full save() here would clobber
        whatever it changed."""
        from franktheunicorn.core.models import PullRequest as PullRequestModel

        runner.record_agent_run(db_pr, "claude", status="ok", findings=2)
        PullRequestModel.objects.filter(pk=db_pr.pk).update(title="renamed upstream")

        runner.record_agent_run(db_pr, "llm", status="ok", findings=0)

        fresh = PullRequestModel.objects.get(pk=db_pr.pk)
        assert fresh.title == "renamed upstream"
        assert set(fresh.agent_runs) == {"claude", "llm"}
        assert fresh.agent_runs["claude"]["findings"] == 2

    def test_recording_never_raises(self, db_pr: PullRequest) -> None:
        """Losing bookkeeping is worth a log line, not a failed review."""
        with patch(
            "franktheunicorn.core.models.PullRequest.objects",
            side_effect=RuntimeError("db gone"),
        ):
            runner.record_agent_run(db_pr, "claude", status="ok")  # must not raise

    def test_the_backfill_no_longer_filters_on_draft_count(self, db_pr: PullRequest) -> None:
        """The per-PR decision belongs to review_skip_reason, which can see the
        run records; the query filtering on drafts pre-empted it wrongly."""
        from tests.factories import ReviewDraftFactory

        ReviewDraftFactory(pull_request=db_pr)
        runner.record_agent_run(db_pr, runner.LLM_REVIEW_SOURCE, status="ok", findings=1)
        pc = ProjectConfig(owner="apache", repo="spark", auto_review_policy="all")
        seen: list[int] = []

        with (
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
            patch(
                "franktheunicorn.worker.runner.process_pr",
                side_effect=lambda pr, *a, **k: seen.append(pr.pk) or [],
            ),
        ):
            runner._backfill_unreviewed_prs(
                already_polled_pks=set(),
                project_configs=[pc],
                operator_config=self._oc(),
                disabled_backends=frozenset(),
                diff_http=MagicMock(),
            )

        assert seen == [db_pr.pk], "a PR missing one reviewer must reach the backfill"


class TestSearchPrsReviewedBy:
    """The `reviewed-by:@me` set — PRs the operator has already judged."""

    @pytest.fixture
    def client(self) -> GitHubClient:
        c = GitHubClient(token="t", base_url="https://api.github.test")
        yield c
        c.close()

    def test_queries_reviewed_by(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json={"items": [_item("apache", "spark", 11)]})

        items = client.search_prs_reviewed_by("holdenk")

        assert [i["number"] for i in items] == [11]
        query = httpx_mock.get_requests()[0].url.params["q"]
        assert "reviewed-by:holdenk" in query
        assert "type:pr" in query
        assert "state:open" in query

    def test_the_username_is_resolved_not_left_as_at_me(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        """`@me` works in GitHub's UI filter and not in the REST search API."""
        httpx_mock.add_response(json={"items": []})

        client.search_prs_reviewed_by("holdenk")

        assert "@me" not in httpx_mock.get_requests()[0].url.params["q"]

    def test_other_forges_return_nothing(self) -> None:
        from franktheunicorn.backends.gitea import GiteaClient

        gitea = GiteaClient(token="t", base_url="https://git.example.test")
        try:
            assert gitea.search_prs_reviewed_by("holdenk") == []
        finally:
            gitea.close()
