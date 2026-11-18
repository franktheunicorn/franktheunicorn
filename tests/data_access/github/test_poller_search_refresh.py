"""The operator scan's half of the poller, and the poll's parallel refresh.

The scan finds PRs by issue search, not by listing. It used to give every hit
the dashboard's on-demand ingest every cycle: a full set of per-PR reads whether
or not anything had changed, a score computed without blame that overwrote the
poll's, no stale-marking on a push, and no ``last_polled_at``. These pin the
replacement: the poll's own unchanged test on the hit, and the poll's own
refresh when it moved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from franktheunicorn.backends.poller import (
    plan_search_hit_refresh,
    poll_project,
    refresh_pull_request_by_number,
)
from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Project, PullRequest
from tests.data_access.github.test_poller import (
    _CountingMockClient,
    _listed_pr,
    _same_repo_detail,
)


class _DetailClient(_CountingMockClient):
    """The real detail endpoint is a superset of the listing entry."""

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        self.detail_calls.append(pr_number)
        listed = next((p for p in self.pr_list if p["number"] == pr_number), _listed_pr(pr_number))
        return listed | _same_repo_detail() | {"number": pr_number, "additions": 31}


def _search_hit(number: int = 99, **overrides: Any) -> dict[str, Any]:
    """What /search/issues returns for a PR: no head, no requested reviewers."""
    hit = _listed_pr(number, **overrides)
    for key in ("head", "requested_reviewers", "id"):
        hit.pop(key, None)
    hit["pull_request"] = {"url": f"https://api.github.test/repos/apache/spark/pulls/{number}"}
    hit["repository_url"] = "https://api.github.test/repos/apache/spark"
    return hit


def _config(**overrides: Any) -> ProjectConfig:
    return ProjectConfig(owner="apache", repo="spark", poll_refresh_hours=24, **overrides)


def _settle(numbers: tuple[int, ...] = (99,)) -> None:
    """A scorer that found nothing reads as degraded; this test is about the skip."""
    PullRequest.objects.filter(number__in=numbers).update(score_breakdown={"stub": 1})


@pytest.mark.django_db
class TestPlanSearchHitRefresh:
    def test_a_never_seen_hit_is_refreshed_and_its_project_row_made(self, tmp_path: Path) -> None:
        client = _DetailClient(tmp_path, [])

        plan = plan_search_hit_refresh(client, _config(), [_search_hit()])

        assert plan.refresh == [99]
        assert plan.unchanged == []
        assert Project.objects.filter(owner="apache", repo="spark").exists()

    def test_a_pr_the_poll_just_refreshed_is_not_fetched_again(self, tmp_path: Path) -> None:
        """The scan runs first in a cycle and again mid-cycle; whichever got there
        first, the other must see an unchanged PR — not every hit, every time."""
        client = _DetailClient(tmp_path, [_listed_pr()])
        poll_project(client, _config(), operator_username="holdenk")
        _settle()

        plan = plan_search_hit_refresh(client, _config(), [_search_hit()])

        assert plan.refresh == []
        assert plan.unchanged == [99]

    def test_the_fields_a_hit_lacks_are_not_read_as_changes(self, tmp_path: Path) -> None:
        """A search hit has no head and no requested reviewers. Compared as empty,
        a PR with a stored head or a reviewer would look changed forever."""
        client = _DetailClient(tmp_path, [_listed_pr(requested_reviewers=[{"login": "holdenk"}])])
        poll_project(client, _config(), operator_username="holdenk")
        _settle()
        assert PullRequest.objects.get(number=99).head_sha, "precondition: a head is stored"

        plan = plan_search_hit_refresh(client, _config(), [_search_hit()])

        assert plan.unchanged == [99]

    def test_a_moved_pr_is_refreshed(self, tmp_path: Path) -> None:
        client = _DetailClient(tmp_path, [_listed_pr()])
        poll_project(client, _config(), operator_username="holdenk")
        _settle()

        plan = plan_search_hit_refresh(
            client, _config(), [_search_hit(updated_at="2026-04-02T11:00:00Z")]
        )

        assert plan.refresh == [99]

    def test_a_field_the_hit_carries_is_still_compared(self, tmp_path: Path) -> None:
        client = _DetailClient(tmp_path, [_listed_pr()])
        poll_project(client, _config(), operator_username="holdenk")
        _settle()

        plan = plan_search_hit_refresh(
            client, _config(), [_search_hit(labels=[{"name": "needs-rebase"}])]
        )

        assert plan.refresh == [99]

    def test_your_own_pr_comes_due_after_an_hour(self, tmp_path: Path) -> None:
        """mergeable moves without the PR moving; only the detail read sees it."""
        client = _DetailClient(tmp_path, [_listed_pr()])
        poll_project(client, _config(), operator_username="holdenk")
        _settle()
        PullRequest.objects.filter(number=99).update(
            is_operator_pr=True, last_polled_at=timezone.now() - timezone.timedelta(hours=2)
        )

        plan = plan_search_hit_refresh(client, _config(), [_search_hit()])

        assert plan.refresh == [99]


@pytest.mark.django_db
class TestRefreshPullRequestByNumber:
    def test_it_is_the_polls_refresh_from_one_detail_read(self, tmp_path: Path) -> None:
        client = _DetailClient(tmp_path, [_listed_pr()])
        plan = plan_search_hit_refresh(client, _config(), [_search_hit()])

        pr = refresh_pull_request_by_number(client, plan.project, _config(), 99, "holdenk")

        assert client.detail_calls == [99], "the detail payload is reused, not re-read"
        assert pr.last_polled_at is not None, "a full refresh stamps the timer"
        assert pr.head_sha and pr.base_sha
        assert pr.additions == 31

    def test_after_it_the_same_hit_is_unchanged(self, tmp_path: Path) -> None:
        client = _DetailClient(tmp_path, [_listed_pr()])
        plan = plan_search_hit_refresh(client, _config(), [_search_hit()])
        refresh_pull_request_by_number(client, plan.project, _config(), 99, "holdenk")
        _settle()

        again = plan_search_hit_refresh(client, _config(), [_search_hit()])

        assert again.unchanged == [99]

    def test_a_push_it_sees_marks_the_earlier_review_stale(self, tmp_path: Path) -> None:
        """The on-demand ingest never did, so a PR outside the listing was never
        re-reviewed after a push."""
        client = _DetailClient(tmp_path, [_listed_pr()])
        config = _config(review_activity_days=0)
        plan = plan_search_hit_refresh(client, config, [_search_hit()])
        refresh_pull_request_by_number(client, plan.project, config, 99, "holdenk")
        PullRequest.objects.filter(number=99).update(
            agent_runs={"llm": {"status": "ok", "findings": 1}}
        )

        original = client.get_pull_request

        def pushed(owner: str, repo: str, pr_number: int) -> dict[str, Any]:
            detail = original(owner, repo, pr_number)
            detail["head"] = {**detail["head"], "sha": "f" * 40}
            return detail

        client.get_pull_request = pushed  # type: ignore[method-assign]
        pr = refresh_pull_request_by_number(client, plan.project, config, 99, "holdenk")

        assert pr.head_sha == "f" * 40
        assert PullRequest.objects.get(number=99).agent_runs["llm"]["stale"] is True


@pytest.mark.django_db
class TestParallelPoll:
    """concurrency > 1 refreshes on worker threads; the answers must not change.

    The per-PR refresh is faked on the worker side: the test database is
    in-memory shared-cache SQLite, which fails concurrent writers outright
    ("database table is locked") where the real file database (WAL, a busy
    timeout, IMMEDIATE transactions) makes them wait — as it already does for
    the ten review runners.
    """

    def test_every_changed_pr_is_refreshed_off_thread_and_returned_in_order(
        self, tmp_path: Path
    ) -> None:
        import threading

        client = _DetailClient(tmp_path, [_listed_pr(n) for n in range(1, 13)])
        threads: set[str] = set()
        lock = threading.Lock()

        def fake_refresh(_c: Any, _p: Any, _pc: Any, pr_data: dict[str, Any], *a: Any, **k: Any):
            with lock:
                threads.add(threading.current_thread().name)
            return MagicMock(number=pr_data["number"], queue="normal")

        with (
            patch("franktheunicorn.backends.poller._refresh_pull_request", fake_refresh),
            patch("franktheunicorn.backends.poller._reroute_pull_requests"),
        ):
            got = poll_project(client, _config(), operator_username="holdenk", concurrency=4)

        assert [pr.number for pr in got] == list(range(1, 13))
        assert threading.current_thread().name not in threads
        assert all(name.startswith("poll") for name in threads)

    def test_the_bail_out_still_stops_spending_and_hands_back_the_tail(
        self, tmp_path: Path
    ) -> None:
        import threading

        client = _DetailClient(tmp_path, [_listed_pr(n) for n in range(1, 21)])
        poll_project(client, _config(), "holdenk")
        client.pr_list = [_listed_pr(n, updated_at="2026-04-09T11:00:00Z") for n in range(1, 21)]
        attempted: list[int] = []
        lock = threading.Lock()

        def failing_refresh(_c: Any, _p: Any, _pc: Any, pr_data: dict[str, Any], *a: Any, **k: Any):
            with lock:
                attempted.append(pr_data["number"])
            msg = "project-wide fault"
            raise RuntimeError(msg)

        skipped: list[PullRequest] = []
        with (
            patch("franktheunicorn.backends.poller._refresh_pull_request", failing_refresh),
            patch("franktheunicorn.backends.poller._rewind_refresh_marker"),
        ):
            got = poll_project(client, _config(), "holdenk", skipped_prs=skipped, concurrency=3)

        assert got == []
        # Five failures trip it; everything after the fifth comes back as skipped.
        assert len(skipped) == 15
        # And it stops spending: the five, plus at most the two in flight beside
        # the fifth — not the whole listing.
        assert len(attempted) <= 5 + 2
