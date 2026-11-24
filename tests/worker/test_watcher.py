"""The watcher thread: per-project watermarks, the notifications/search choice
per watched user, and the handoff to the running cycle."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.backends.base import (
    ForgeClient,
    NotificationPage,
    NotificationsUnavailableError,
    UpdatedPullRequests,
)
from franktheunicorn.backends.github import parse_github_timestamp
from franktheunicorn.backends.poller import RecentSync
from franktheunicorn.config.models import OperatorConfig, ProjectConfig, WatchConfig
from franktheunicorn.worker import runner
from franktheunicorn.worker.watcher import WATERMARK_OVERLAP, Watcher

SPARK = ProjectConfig(owner="apache", repo="spark")
T0 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def _client(login: str = "holdenk", *, updated_since: bool = True) -> MagicMock:
    client = MagicMock(spec=ForgeClient)
    client.supports_updated_since = updated_since
    client.get_authenticated_user.return_value = {"login": login}
    client.list_notifications.return_value = NotificationPage([], 60)
    client.list_pull_requests_updated_since.return_value = UpdatedPullRequests(
        [{"number": 1, "updated_at": "2026-11-24T10:00:00Z"}], True
    )
    return client


def _watcher(
    client: MagicMock | None = None,
    *,
    projects: list[Any] | None = None,
    token_client: MagicMock | None = None,
    **watch: Any,
) -> Watcher:
    oc = OperatorConfig(github_username="holdenk", watch=watch)
    return Watcher(
        {"github": client if client is not None else _client()},
        oc,
        load_projects=lambda: projects if projects is not None else [SPARK],
        client_for_token=(lambda entry: token_client) if token_client is not None else None,
    )


def _pr(number: int, *, state: str = "open", own: bool = False) -> MagicMock:
    return MagicMock(pk=number, number=number, state=state, is_operator_pr=own)


class TestWatchConfig:
    def test_no_users_watches_the_operator_automatically(self) -> None:
        users = WatchConfig().users_for("holdenk")
        assert [(u.user, u.source) for u in users] == [("holdenk", "auto")]

    def test_blank_user_is_the_operator_and_duplicates_collapse(self) -> None:
        watch = WatchConfig(users=[{"source": "notifications"}, {"user": "HoldenK"}])
        assert [(u.user, u.source) for u in watch.users_for("holdenk")] == [
            ("holdenk", "notifications")
        ]

    def test_listing_a_co_maintainer_still_watches_the_operator_first(self) -> None:
        """Otherwise the searches "Your PRs" is built on quietly stop."""
        watch = WatchConfig(users=[{"user": "cloud-fan", "source": "search"}])
        assert [(u.user, u.source) for u in watch.users_for("holdenk")] == [
            ("holdenk", "auto"),
            ("cloud-fan", "search"),
        ]

    def test_without_an_operator_a_blank_user_is_dropped(self) -> None:
        assert WatchConfig().users_for("") == []

    def test_negative_intervals_are_refused(self) -> None:
        with pytest.raises(ValueError):
            WatchConfig(interval_seconds=-1)


class TestResolveUsers:
    def test_search_is_search(self) -> None:
        client = _client()
        watcher = _watcher(client, users=[{"source": "search"}])
        watcher.tick()
        assert not watcher.reads_notifications("github", "holdenk")
        client.list_notifications.assert_not_called()

    def test_auto_uses_the_polling_token_when_it_is_the_users(self) -> None:
        watcher = _watcher(_client("HoldenK"))
        watcher.tick()
        assert watcher.reads_notifications("github", "holdenk")

    def test_a_token_belonging_to_someone_else_is_not_read_as_theirs(self, caplog: Any) -> None:
        """Notifications are the token owner's inbox; reading a bot's as the
        operator's would watch the wrong person."""
        watcher = _watcher(_client("frank-bot"), users=[{"source": "notifications"}])
        with caplog.at_level(logging.WARNING):
            watcher.tick()
        assert not watcher.reads_notifications("github", "holdenk")
        assert "frank-bot" in caplog.text
        assert "classic PAT" in caplog.text

    def test_a_fine_grained_token_falls_back_to_search(self, caplog: Any) -> None:
        client = _client()
        client.list_notifications.side_effect = NotificationsUnavailableError(403, "nope")
        watcher = _watcher(client)
        with caplog.at_level(logging.INFO):
            watcher.tick()
        assert not watcher.reads_notifications("github", "holdenk")
        assert "fine-grained" in caplog.text

    def test_auto_says_so_at_info_explicit_at_warning(self, caplog: Any) -> None:
        client = _client("frank-bot")
        with caplog.at_level(logging.INFO):
            _watcher(client).tick()
        assert all(r.levelno == logging.INFO for r in caplog.records if "frank-bot" in r.message)

    def test_a_forge_without_notifications_falls_back(self) -> None:
        client = _client()
        client.list_notifications.side_effect = NotImplementedError
        watcher = _watcher(client)
        watcher.tick()
        assert not watcher.reads_notifications("github", "holdenk")

    def test_a_transient_probe_failure_is_retried_next_check(self) -> None:
        """A network blip at startup must not cost notifications until a restart."""
        client = _client()
        client.list_notifications.side_effect = [
            TimeoutError("slow"),
            NotificationPage([], 60),
            NotificationPage([], 60),
        ]
        watcher = _watcher(client, projects=[])
        watcher.tick()
        assert not watcher.reads_notifications("github", "holdenk"), "searched meanwhile"
        watcher.tick()
        assert watcher.reads_notifications("github", "holdenk")

    def test_a_users_own_token_gets_its_own_client(self) -> None:
        polling = _client("frank-bot")
        mine = _client("cloud-fan")
        watcher = _watcher(
            polling,
            token_client=mine,
            users=[{"user": "cloud-fan", "source": "notifications", "token": "ghp_x"}],
        )
        watcher.tick()
        assert watcher.reads_notifications("github", "cloud-fan")
        polling.list_notifications.assert_not_called()
        watcher.close()
        mine.close.assert_called_once()

    def test_an_unbuildable_token_client_falls_back(self) -> None:
        watcher = _watcher(users=[{"source": "notifications", "token": "ghp_x"}])
        watcher.tick()
        assert not watcher.reads_notifications("github", "holdenk")

    def test_a_failed_whoami_is_retried_too(self) -> None:
        client = _client()
        client.get_authenticated_user.side_effect = [RuntimeError("502"), {"login": "holdenk"}]
        watcher = _watcher(client, projects=[])
        watcher.tick()
        assert not watcher.reads_notifications("github", "holdenk")
        watcher.tick()
        assert watcher.reads_notifications("github", "holdenk")


@pytest.mark.django_db
class TestProjects:
    def test_the_first_look_only_sets_the_watermark(self) -> None:
        """The cycle starts with a full poll; acting now would race it."""
        with patch("franktheunicorn.backends.poller.sync_recent_pull_requests") as sync:
            watcher = _watcher(users=[{"source": "search"}])
            watcher.tick()
        sync.assert_not_called()
        assert watcher._watermarks["apache/spark"] == T0 - WATERMARK_OVERLAP

    def test_later_looks_sync_from_the_watermark_and_hand_off_open_prs(self) -> None:
        watcher = _watcher(users=[{"source": "search"}, {"user": "cloud-fan", "source": "search"}])
        watcher._watermarks["apache/spark"] = T0
        result = RecentSync(
            closed=1,
            new=[_pr(5, own=True)],
            refreshed=[_pr(6), _pr(7, state="closed")],
            newest=T0 + timedelta(minutes=10),
            resume_from=T0 + timedelta(minutes=10),
        )
        with patch(
            "franktheunicorn.backends.poller.sync_recent_pull_requests", return_value=result
        ) as sync:
            watcher.tick()

        assert sync.call_args.args[3] == T0
        assert sync.call_args.kwargs["watched_users"] == ["holdenk", "cloud-fan"]
        handed = watcher.drain_for_review()
        assert [(h.pr.number, h.own) for h in handed] == [(5, True), (6, False)]
        assert watcher.drain_for_review() == [], "drained once"
        assert watcher._watermarks["apache/spark"] == T0 + timedelta(minutes=8)

    def test_the_watermark_never_moves_backwards(self) -> None:
        watcher = _watcher(users=[{"source": "search"}])
        watcher._watermarks["apache/spark"] = T0
        with patch(
            "franktheunicorn.backends.poller.sync_recent_pull_requests",
            return_value=RecentSync(newest=T0, resume_from=T0),
        ):
            watcher.tick()
        assert watcher._watermarks["apache/spark"] == T0
        assert watcher.covers(SPARK)

    def test_the_watermark_resumes_where_the_sync_says(self) -> None:
        """A refresh cap that cut PRs off must not be skipped past."""
        watcher = _watcher(users=[{"source": "search"}])
        watcher._watermarks["apache/spark"] = T0
        with patch(
            "franktheunicorn.backends.poller.sync_recent_pull_requests",
            return_value=RecentSync(
                newest=T0 + timedelta(hours=1), resume_from=T0 + timedelta(minutes=30)
            ),
        ):
            watcher.tick()
        assert watcher._watermarks["apache/spark"] == T0 + timedelta(minutes=28)

    def test_a_truncated_walk_hands_the_project_back_to_the_cycle(self) -> None:
        watcher = _watcher(users=[{"source": "search"}])
        watcher._watermarks["apache/spark"] = T0
        watcher._covered.add("apache/spark")
        with patch(
            "franktheunicorn.backends.poller.sync_recent_pull_requests",
            return_value=RecentSync(newest=T0, resume_from=T0, complete=False),
        ):
            watcher.tick()
        assert not watcher.covers(SPARK)

    def test_a_failing_project_goes_back_to_the_cycle_and_says_so_once(self, caplog: Any) -> None:
        watcher = _watcher(users=[{"source": "search"}])
        watcher._watermarks["apache/spark"] = T0
        watcher._covered.add("apache/spark")
        with (
            caplog.at_level(logging.INFO),
            patch(
                "franktheunicorn.backends.poller.sync_recent_pull_requests",
                side_effect=[RuntimeError("401"), RuntimeError("401"), RecentSync()],
            ),
        ):
            watcher.tick()
            assert not watcher.covers(SPARK)
            watcher.tick()
            watcher.tick()
        assert watcher._watermarks["apache/spark"] == T0
        assert watcher.covers(SPARK), "covered again once a check succeeds"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "reading apache/spark again" in caplog.text

    def test_a_failed_first_listing_is_a_warning_not_silence(self, caplog: Any) -> None:
        client = _client()
        client.list_pull_requests_updated_since.side_effect = RuntimeError("401")
        watcher = _watcher(client, users=[{"source": "search"}])
        with caplog.at_level(logging.WARNING):
            watcher.tick()
        assert "could not list apache/spark" in caplog.text
        assert not watcher.covers(SPARK)

    def test_without_a_token_the_project_is_left_to_the_cycle(self, caplog: Any) -> None:
        """A 304 is free only when authenticated; unauthenticated, a check a
        minute is the whole quota."""
        client = _client()
        client.authenticated = False
        watcher = _watcher(client, users=[{"source": "search"}])
        with caplog.at_level(logging.INFO):
            watcher.tick()
            watcher.tick()
        client.list_pull_requests_updated_since.assert_not_called()
        assert not watcher.covers(SPARK)
        assert caplog.text.count("has no token") == 1

    def test_a_forge_that_cannot_answer_is_left_to_the_cycle(self) -> None:
        client = _client(updated_since=False)
        watcher = _watcher(client, users=[{"source": "search"}])
        watcher.tick()
        client.list_pull_requests_updated_since.assert_not_called()
        assert not watcher.covers(SPARK)

    def test_disabled_and_unknown_forge_projects_are_skipped(self) -> None:
        client = _client()
        watcher = _watcher(
            client,
            projects=[
                ProjectConfig(owner="a", repo="off", enabled=False),
                ProjectConfig(owner="a", repo="gl", forge="gitlab"),
                "not a config",
            ],
            users=[{"source": "search"}],
        )
        watcher.tick()
        client.list_pull_requests_updated_since.assert_not_called()


def _stamp(minutes_from_now: int = 0) -> str:
    """Notification times are after the watcher started: since is "now-ish"."""
    from django.utils import timezone

    moment = timezone.now() + timedelta(minutes=minutes_from_now)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _notification(number: int, updated: str = "", **over: Any) -> Any:
    updated = updated or _stamp(1)
    return {
        "id": f"t{number}",
        "reason": "mention",
        "updated_at": updated,
        "subject": {
            "type": "PullRequest",
            "url": f"https://api.github.com/repos/apache/spark/pulls/{number}",
        },
        "repository": {"full_name": "apache/spark"},
    } | over


@pytest.mark.django_db
class TestNotifications:
    def _ready(self, client: MagicMock) -> Watcher:
        watcher = _watcher(client)
        watcher._watermarks["apache/spark"] = T0  # keep the project pass quiet
        with patch(
            "franktheunicorn.backends.poller.sync_recent_pull_requests",
            return_value=RecentSync(),
        ):
            watcher.tick()  # probe
        assert watcher.reads_notifications("github", "holdenk")
        watcher._users[0].next_check = 0  # type: ignore[index]
        return watcher

    def _tick(self, watcher: Watcher, refresh: Any) -> MagicMock:
        with (
            patch(
                "franktheunicorn.backends.poller.sync_recent_pull_requests",
                return_value=RecentSync(),
            ),
            patch(
                "franktheunicorn.backends.poller.refresh_notified_pull_request",
                side_effect=refresh,
            ) as notified,
        ):
            watcher.tick()
        watcher._users[0].next_check = 0  # type: ignore[index]
        return notified

    def test_a_mention_on_a_configured_project_is_refreshed_and_handed_off(self) -> None:
        client = _client()
        watcher = self._ready(client)
        client.list_notifications.return_value = NotificationPage(
            [
                _notification(42),
                _notification(43, repository={"full_name": "someone/else"}),
                _notification(44, subject={"type": "PullRequest", "url": "no-number"}),
            ],
            60,
        )

        notified = self._tick(watcher, lambda c, pc, n, *a, **k: _pr(n))

        assert [c.args[2] for c in notified.call_args_list] == [42]
        assert [h.pr.number for h in watcher.drain_for_review()] == [42]
        newest = client.list_notifications.return_value.items[0]["updated_at"]
        assert watcher._users[0].since == parse_github_timestamp(newest)  # type: ignore[index]

    def test_the_same_notification_is_acted_on_once(self) -> None:
        client = _client()
        watcher = self._ready(client)
        client.list_notifications.return_value = NotificationPage([_notification(42)], 60)

        self._tick(watcher, lambda *a, **k: _pr(42))
        notified = self._tick(watcher, lambda *a, **k: _pr(42))

        notified.assert_not_called()

    def test_a_row_refreshed_after_the_activity_is_not_refreshed_again(self) -> None:
        from django.utils import timezone

        from tests.factories import ProjectFactory, PullRequestFactory

        project = ProjectFactory(owner="apache", repo="spark")
        PullRequestFactory(
            project=project, number=42, last_polled_at=timezone.now() + timedelta(minutes=5)
        )
        client = _client()
        watcher = self._ready(client)
        client.list_notifications.return_value = NotificationPage([_notification(42)], 60)

        notified = self._tick(watcher, lambda *a, **k: _pr(42))

        notified.assert_not_called()

    def test_a_failed_refresh_is_retried_and_holds_the_watermark(self) -> None:
        client = _client()
        watcher = self._ready(client)
        client.list_notifications.return_value = NotificationPage(
            [_notification(42, updated=_stamp(1)), _notification(43, updated=_stamp(5))], 60
        )

        def flaky(c: Any, pc: Any, n: int, *a: Any, **k: Any) -> Any:
            if n == 42:
                msg = "502"
                raise RuntimeError(msg)
            return _pr(n)

        self._tick(watcher, flaky)
        failed_at = parse_github_timestamp(_stamp(1))
        assert failed_at is not None
        assert watcher._users[0].since < failed_at  # type: ignore[index, operator]
        notified = self._tick(watcher, lambda *a, **k: _pr(42))
        assert [c.args[2] for c in notified.call_args_list] == [42]

    def test_a_refresh_that_always_fails_is_given_up_on(self, caplog: Any) -> None:
        """Holding the watermark for a PR the token can't read would re-read the
        same pages and log a traceback every minute forever."""
        from franktheunicorn.worker.watcher import MAX_NOTIFICATION_RETRIES

        client = _client()
        watcher = self._ready(client)
        client.list_notifications.return_value = NotificationPage([_notification(42)], 60)

        def boom(*a: Any, **k: Any) -> Any:
            msg = "404"
            raise RuntimeError(msg)

        with caplog.at_level(logging.WARNING):
            calls = sum(
                self._tick(watcher, boom).call_count for _ in range(MAX_NOTIFICATION_RETRIES + 2)
            )
        assert calls == MAX_NOTIFICATION_RETRIES
        assert "Giving up on apache/spark#42" in caplog.text
        stamp = parse_github_timestamp(
            client.list_notifications.return_value.items[0]["updated_at"]
        )
        assert watcher._users[0].since == stamp  # type: ignore[index]

    def test_a_pr_being_refreshed_elsewhere_is_asked_about_again(self) -> None:
        """That refresh may have read the comments before the mention landed."""
        from franktheunicorn.backends.poller import RefreshInProgressError

        client = _client()
        watcher = self._ready(client)
        client.list_notifications.return_value = NotificationPage([_notification(42)], 60)

        def busy(*a: Any, **k: Any) -> Any:
            raise RefreshInProgressError("apache/spark#42")

        self._tick(watcher, busy)
        notified = self._tick(watcher, lambda *a, **k: _pr(42))
        assert [c.args[2] for c in notified.call_args_list] == [42]
        assert watcher._users[0].failures == {}  # type: ignore[index]

    def test_an_incomplete_read_says_what_covers_the_rest(self, caplog: Any) -> None:
        client = _client()
        watcher = self._ready(client)
        client.list_notifications.return_value = NotificationPage([_notification(42)], 60, False)

        with caplog.at_level(logging.INFO):
            self._tick(watcher, lambda *a, **k: _pr(42))

        assert "left to the searches" in caplog.text

    def test_a_closed_pr_is_not_handed_off(self) -> None:
        client = _client()
        watcher = self._ready(client)
        client.list_notifications.return_value = NotificationPage([_notification(42)], 60)

        self._tick(watcher, lambda *a, **k: None)

        assert watcher.drain_for_review() == []

    def test_the_forges_poll_interval_is_respected(self) -> None:
        client = _client()
        watcher = self._ready(client)
        client.list_notifications.reset_mock()
        watcher._users[0].next_check = float("inf")  # type: ignore[index]
        with patch(
            "franktheunicorn.backends.poller.sync_recent_pull_requests",
            return_value=RecentSync(),
        ):
            watcher.tick()
        client.list_notifications.assert_not_called()

    def test_losing_access_mid_run_switches_to_search(self, caplog: Any) -> None:
        client = _client()
        watcher = self._ready(client)
        client.list_notifications.side_effect = NotificationsUnavailableError(401)

        with caplog.at_level(logging.WARNING):
            self._tick(watcher, lambda *a, **k: None)

        assert not watcher.reads_notifications("github", "holdenk")
        assert "watching by search" in caplog.text

    def test_a_transient_failure_warns_once_and_says_when_it_recovers(self, caplog: Any) -> None:
        client = _client()
        watcher = self._ready(client)
        client.list_notifications.side_effect = [
            TimeoutError("slow"),
            TimeoutError("slow"),
            NotificationPage([], 60),
        ]

        with caplog.at_level(logging.INFO):
            for _ in range(3):
                self._tick(watcher, lambda *a, **k: None)

        assert watcher.reads_notifications("github", "holdenk")
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        assert "being read again" in caplog.text


class TestRunLoop:
    def test_runs_until_stopped_and_survives_a_bad_tick(self) -> None:
        watcher = _watcher(users=[{"source": "search"}], interval_seconds=1)
        stop = threading.Event()
        calls: list[int] = []

        def tick() -> None:
            calls.append(1)
            stop.set()
            msg = "boom"
            raise RuntimeError(msg)

        with patch.object(watcher, "tick", side_effect=tick):
            watcher.run(stop)

        assert calls == [1]


class TestRunnerWiring:
    def test_interval_zero_starts_no_thread(self) -> None:
        oc = OperatorConfig(watch={"interval_seconds": 0})
        assert runner._start_watcher({"github": _client()}, oc, threading.Event()) is None

    def test_no_clients_starts_no_thread(self) -> None:
        assert runner._start_watcher({}, OperatorConfig(), threading.Event()) is None

    @pytest.mark.django_db
    def test_starts_and_stops(self) -> None:
        stop = threading.Event()
        stop.set()  # the first wait returns at once
        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[]),
            patch.object(runner, "_watcher", None),
        ):
            thread = runner._start_watcher(
                {"github": _client()}, OperatorConfig(github_username="holdenk"), stop
            )
            assert thread is not None
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert isinstance(runner._watcher, Watcher)

    def test_the_mid_cycle_relisting_leaves_watched_forges_alone(self) -> None:
        covered = _client()
        watcher = MagicMock()
        watcher.covers.side_effect = lambda pc: pc is SPARK
        runner._last_closed_reap = float("-inf")
        with (
            patch.object(runner, "_watcher", watcher),
            patch("franktheunicorn.backends.poller.sync_open_pull_requests") as sync,
        ):
            runner._reap_closed_prs_if_due({"github": covered}, [SPARK], "holdenk")
        sync.assert_not_called()

    def test_the_cycle_queues_what_the_watcher_handed_off(self) -> None:
        from franktheunicorn.worker.review_pool import ReviewPool
        from franktheunicorn.worker.watcher import ReviewHandoff

        watcher = MagicMock()
        watcher.reads_notifications.return_value = False
        watcher.drain_for_review.side_effect = [
            [ReviewHandoff(_pr(1, own=True), SPARK, None, own=True)],
            [ReviewHandoff(_pr(1, own=True), SPARK, None, own=True)],  # again: not twice
            [ReviewHandoff(_pr(2), SPARK, None, own=False)],
        ] + [[]] * 50
        submitted: list[tuple[int, bool]] = []

        def record(_self: Any, item: Any, *, front: bool = False) -> None:
            submitted.append((item.pr.number, front))

        oc = OperatorConfig(github_username="holdenk", worker_pool={"operator_scan_minutes": 0})
        with (
            patch.object(runner, "_watcher", watcher),
            patch.object(runner, "_scan_operator_prs", return_value=[]),
            patch.object(ReviewPool, "submit", record),
            patch.object(runner, "_run_shepherding_pass"),
            patch.object(runner, "_fetch_dependency_changelogs_for_cycle"),
            patch.object(runner, "_backfill_unreviewed_prs"),
            patch.object(runner, "_drain_worker_commands"),
        ):
            runner._run_cycle({}, [], "holdenk", oc)

        assert submitted == [(1, True), (2, False)]
