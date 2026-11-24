"""The watcher: new, closed and involved-in PRs within a minute, beside the cycle.

Change used to be found only from the poll cycle's main thread: the listing at
the top of a cycle, a full re-listing every ten minutes from the cycle's turns,
and the operator searches every ten. A Spark cycle runs for hours and its turns
are uneven, so a new PR, a merge, or a mention waited anywhere from ten minutes
to most of a day — and every re-listing re-downloaded every open PR.

This thread asks each project's forge "what changed since I last asked?" every
``watch.interval_seconds`` — one request, and with the client's conditional
requests a free 304 when nothing did (``poller.sync_recent_pull_requests``). It
writes what it finds straight to the database, so the dashboard has it at once,
and hands PRs that want a review to the running cycle, which queues them at its
next turn (``drain_for_review``).

It also reads GitHub notifications, for every watched user whose ``source``
says so (``WatchedUserConfig``). Which users, and whether each is read through
notifications or searched, is configuration; ``auto`` probes once and logs what
it settled on (and retries a probe that failed for a transient reason). A user
read through notifications is still searched every
``watch.search_backstop_minutes``: notifications say what moved, the searches
say what the whole set is.

A project counts as covered — so the cycle's ten-minute full re-listing leaves
it alone — only while its last check here succeeded and read everything that
moved. A failing, truncated, or unauthenticated project goes back to the cycle.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from django.db import connection
from django.utils import timezone

from franktheunicorn.backends.base import ForgeClient, NotificationsUnavailableError
from franktheunicorn.backends.github import _is_rate_limit_response
from franktheunicorn.backends.poller import RecentSync
from franktheunicorn.config.models import (
    OperatorConfig,
    ProjectConfig,
    WatchConfig,
    WatchedUserConfig,
)
from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)

#: How far behind the newest ``updated_at`` seen the next listing starts. The
#: forge's listing is eventually consistent; an update stamped a moment before
#: the last one we saw can appear a moment after. Re-reading the overlap is free:
#: an unchanged PR is skipped and an already-closed row matches nothing.
WATERMARK_OVERLAP = timedelta(minutes=2)

#: Where the first notifications check starts: a little before the worker did,
#: so a mention that arrived during a restart is still seen.
NOTIFICATION_LOOKBACK = timedelta(minutes=15)

#: Failed refreshes of one notification before the watcher stops retrying it and
#: lets its watermark move on. The poll and the searches still find the PR; a
#: notification that can never be refreshed (a PR the polling token can't read)
#: must not hold the watermark back and log a traceback every minute forever.
MAX_NOTIFICATION_RETRIES = 3

#: Pages (of 30 PRs) a normal check walks, and a catch-up walk after a normal
#: one fell short of the watermark — ~300 and ~1200 moved PRs.
NORMAL_PAGES = 10
CATCH_UP_PAGES = 40


@dataclass
class ReviewHandoff:
    """A PR the watcher refreshed that the cycle should consider reviewing."""

    pr: PullRequest
    pc: ProjectConfig
    client: object
    own: bool


@dataclass
class UserWatch:
    """How one watched user's involvement is being found.

    ``mode`` is ``notifications``, ``search``, or ``probing`` (searched while a
    probe that failed for a transient reason is retried each check).
    """

    entry: WatchedUserConfig
    mode: str
    client: ForgeClient | None = None
    owns_client: bool = False
    since: datetime | None = None
    next_check: float = 0.0
    #: Whether the last notifications read failed (for say-it-once logging).
    failing: bool = False
    #: Notification thread id → the updated_at last acted on.
    seen: dict[str, str] = field(default_factory=dict)
    #: Notification thread id → failed refreshes so far (MAX_NOTIFICATION_RETRIES).
    failures: dict[str, int] = field(default_factory=dict)

    @property
    def user(self) -> str:
        return self.entry.user

    @property
    def forge(self) -> str:
        return self.entry.notifications_forge


ClientFactory = Callable[[WatchedUserConfig], ForgeClient | None]


class Watcher:
    """Owns the watermarks and user probes; ``run`` is the thread's body."""

    def __init__(
        self,
        clients: Mapping[str, object],
        operator_config: OperatorConfig,
        *,
        load_projects: Callable[[], Sequence[object]],
        clone_path: Callable[[ProjectConfig], Path | None] = lambda pc: None,
        client_for_token: ClientFactory | None = None,
    ) -> None:
        self._clients = clients
        self._oc = operator_config
        self._load_projects = load_projects
        self._clone_path = clone_path
        self._client_for_token = client_for_token
        self._watermarks: dict[str, datetime] = {}
        #: Projects whose last check succeeded and was complete.
        self._covered: set[str] = set()
        #: Projects already told about (unauthenticated, failing), to say it once.
        self._said: set[tuple[str, str]] = set()
        self._users: list[UserWatch] | None = None
        self._handoff: deque[ReviewHandoff] = deque()
        self._lock = threading.Lock()

    @property
    def config(self) -> WatchConfig:
        return self._oc.watch

    # --- what the cycle reads ------------------------------------------------

    def drain_for_review(self) -> list[ReviewHandoff]:
        """Everything handed off since the last drain, oldest first."""
        with self._lock:
            items = list(self._handoff)
            self._handoff.clear()
        return items

    def reads_notifications(self, forge: str, user: str) -> bool:
        """Whether *user*'s involvement is being found through notifications."""
        for watch in self._users or []:
            if watch.forge == (forge or "github") and watch.user.lower() == user.lower():
                return watch.mode == "notifications"
        return False

    def covers(self, project: ProjectConfig) -> bool:
        """Whether this watcher is reading *project*'s changes right now, so the
        mid-cycle full re-listing can leave it alone."""
        with self._lock:
            return project.full_name in self._covered

    # --- the thread ------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        interval = max(1, self.config.interval_seconds)
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                logger.exception("Watcher check failed; trying again in %ds", interval)
            finally:
                # Per-thread connection: an idle SQLite handle held across the
                # minute between checks is one more holder of the shared file.
                connection.close()
            stop.wait(max(0.0, interval - (time.monotonic() - started)))
        self.close()

    def tick(self) -> None:
        project_configs = [
            pc
            for pc in self._load_projects()
            if isinstance(pc, ProjectConfig) and pc.enabled and pc.forge in self._clients
        ]
        if self._users is None:
            self._users = self._resolve_users()
        else:
            self._retry_probes()
        self._check_projects(project_configs)
        self._check_notifications(project_configs)

    def close(self) -> None:
        for watch in self._users or []:
            if watch.owns_client and watch.client is not None:
                try:
                    watch.client.close()
                except Exception:
                    logger.debug("Error closing notifications client", exc_info=True)

    # --- projects --------------------------------------------------------------

    def _set_covered(self, pc: ProjectConfig, covered: bool) -> None:
        with self._lock:
            if covered:
                self._covered.add(pc.full_name)
            else:
                self._covered.discard(pc.full_name)

    def _say_once(
        self,
        key: tuple[str, str],
        level: int,
        msg: str,
        *args: object,
        exc_info: bool = False,
    ) -> None:
        """Log once per (project, condition) until the condition clears."""
        if key in self._said:
            logger.debug(msg, *args, exc_info=exc_info)
            return
        self._said.add(key)
        logger.log(level, msg, *args, exc_info=exc_info)

    def _cleared(self, key: tuple[str, str], msg: str, *args: object) -> None:
        if key in self._said:
            self._said.discard(key)
            logger.info(msg, *args)

    def _check_projects(self, project_configs: Sequence[ProjectConfig]) -> None:
        watched = [w.user for w in self._users or []]
        for pc in project_configs:
            client = self._clients[pc.forge]
            if not isinstance(client, ForgeClient) or client.supports_updated_since is not True:
                self._set_covered(pc, False)
                continue
            if getattr(client, "authenticated", True) is False:
                # A 304 is free only for an authenticated request: unauthenticated,
                # a check a minute is the whole 60-an-hour quota for one project.
                self._set_covered(pc, False)
                self._say_once(
                    (pc.full_name, "no-token"),
                    logging.INFO,
                    "Watcher leaves %s to the cycle: forge %r has no token, and without one "
                    "every check costs a request from GitHub's 60-an-hour unauthenticated "
                    "quota",
                    pc.full_name,
                    pc.forge,
                )
                continue
            since = self._watermarks.get(pc.full_name)
            if since is None:
                self._establish_watermark(pc, client)
                continue
            try:
                result = self._sync_project(pc, client, since, watched)
            except Exception:
                self._set_covered(pc, False)
                self._say_once(
                    (pc.full_name, "failing"),
                    logging.WARNING,
                    "Watcher could not read %s's changes; the cycle's ten-minute re-listing "
                    "covers it until a check succeeds",
                    pc.full_name,
                    exc_info=True,
                )
                continue
            self._cleared((pc.full_name, "failing"), "Watcher reading %s again", pc.full_name)
            if result.resume_from is not None:
                self._watermarks[pc.full_name] = max(since, result.resume_from - WATERMARK_OVERLAP)
            # Still incomplete after the deep walk and the re-listing: hand the
            # project back to the cycle until a check reads everything again.
            self._set_covered(pc, result.complete)
            if result.closed or result.new or result.refreshed:
                logger.info(
                    "Watcher %s: %d new%s, %d closed/merged, %d involved PR(s) refreshed, "
                    "%d other change(s) left to the poll",
                    pc.full_name,
                    len(result.new),
                    f" ({', '.join(f'#{pr.number}' for pr in result.new)})" if result.new else "",
                    result.closed,
                    len(result.refreshed),
                    result.deferred,
                )
            for pr in [*result.new, *result.refreshed]:
                if pr.state == "open":
                    self._hand_off(pr, pc, client)

    def _sync_project(
        self, pc: ProjectConfig, client: ForgeClient, since: datetime, watched: list[str]
    ) -> RecentSync:
        """One check of *pc*, catching up when more moved than a check reads.

        An incomplete walk (more than ~300 PRs moved since the watermark — after
        an outage, say) is walked again at once, deeper. If even that doesn't
        reach the watermark, the full open-PR re-listing runs here and now — it
        closes what left the listing and ingests the newest new PRs — rather than
        waiting for a cycle turn that may be hours off. Either way the watermark
        then moves to the newest seen, so the next check is a normal one.
        """
        from franktheunicorn.backends.poller import (
            sync_open_pull_requests,
            sync_recent_pull_requests,
        )

        def sync(max_pages: int) -> RecentSync:
            return sync_recent_pull_requests(
                pc,
                client,
                self._oc.github_username,
                since,
                watched_users=watched,
                repo_path=self._clone_path(pc),
                merge_intent=self._oc.merge_intent,
                concurrency=max(1, self._oc.worker_pool.refresh_concurrency),
                max_refreshes=self.config.max_refreshes_per_check,
                max_pages=max_pages,
            )

        result = sync(NORMAL_PAGES)
        if result.complete:
            return result
        logger.info(
            "%s: more PRs moved since %s than one check reads; walking deeper",
            pc.full_name,
            since.isoformat(timespec="seconds"),
        )
        deep = sync(CATCH_UP_PAGES)
        merged = _merge(result, deep)
        if deep.complete:
            return merged
        try:
            closed, new = sync_open_pull_requests(
                pc,
                client,
                self._oc.github_username,
                repo_path=self._clone_path(pc),
                merge_intent=self._oc.merge_intent,
                concurrency=max(1, self._oc.worker_pool.refresh_concurrency),
            )
        except Exception:
            logger.warning(
                "%s: the catch-up re-listing failed too; the cycle covers it until a "
                "check reads everything again",
                pc.full_name,
                exc_info=True,
            )
            # Keep the watermark where it was: the next check tries to catch up
            # again instead of stepping past what it never read.
            merged.resume_from = None
            return merged
        logger.info(
            "%s: caught up from a full re-listing (%d closed, %d new)",
            pc.full_name,
            closed,
            len(new),
        )
        known = {pr.pk for pr in merged.new}
        merged.new.extend(pr for pr in new if pr.pk not in known)
        merged.closed += closed
        merged.complete = True
        merged.resume_from = merged.newest
        return merged

    def _establish_watermark(self, pc: ProjectConfig, client: ForgeClient) -> None:
        """First sight of a project: note where "now" is, act on nothing.

        The cycle starts with a full poll of every project, so acting on the
        first page here would only race it for the same PRs.
        """
        from franktheunicorn.backends.github import parse_github_timestamp

        try:
            listing = client.list_pull_requests_updated_since(pc.owner, pc.repo, None)
        except Exception:
            self._say_once(
                (pc.full_name, "failing"),
                logging.WARNING,
                "Watcher could not list %s; the cycle's ten-minute re-listing covers it "
                "until a check succeeds",
                pc.full_name,
                exc_info=True,
            )
            return
        self._cleared((pc.full_name, "failing"), "Watcher reading %s again", pc.full_name)
        stamps = [parse_github_timestamp(i.get("updated_at")) for i in listing.items]
        newest = max((s for s in stamps if s is not None), default=None)
        self._watermarks[pc.full_name] = (newest or timezone.now()) - WATERMARK_OVERLAP
        self._set_covered(pc, True)
        logger.debug("Watcher watching %s from %s", pc.full_name, self._watermarks[pc.full_name])

    def _hand_off(self, pr: PullRequest, pc: ProjectConfig, client: object) -> None:
        with self._lock:
            self._handoff.append(
                ReviewHandoff(pr, pc, client, own=bool(getattr(pr, "is_operator_pr", False)))
            )

    # --- users -----------------------------------------------------------------

    def _resolve_users(self) -> list[UserWatch]:
        """Probe each watched user: notifications if configured and possible."""
        resolved: list[UserWatch] = []
        for entry in self.config.users_for(self._oc.github_username):
            watch = UserWatch(entry, "search")
            if entry.source != "search":
                watch.mode = "probing"
                self._probe(watch)
            resolved.append(watch)
        if resolved:
            logger.info(
                "Watching %s",
                "; ".join(
                    f"{w.user} via {w.mode}"
                    + (
                        f" (searched every {self.config.search_backstop_minutes} min too)"
                        if w.mode == "notifications"
                        else " (search meanwhile; retrying)"
                        if w.mode == "probing"
                        else ""
                    )
                    for w in resolved
                ),
            )
        return resolved

    def _retry_probes(self) -> None:
        for watch in self._users or []:
            if watch.mode == "probing":
                self._probe(watch)
                if watch.mode == "notifications":
                    logger.info("Watching %s via notifications now", watch.user)

    def _probe(self, watch: UserWatch) -> None:
        """Settle *watch*'s mode: notifications, search for good, or still probing.

        A definitive answer (no client, someone else's token, a token that can't
        read notifications, a forge without them) settles on search. A transient
        failure leaves it probing, searched meanwhile, and the next check retries
        — a network blip at startup must not cost the user notifications until
        a restart.
        """
        entry = watch.entry
        first = watch.client is None
        explicit = entry.source == "notifications"
        say = logger.warning if explicit else logger.info

        client = watch.client
        if client is None:
            if entry.token:
                client = self._client_for_token(entry) if self._client_for_token else None
                watch.owns_client = client is not None
            else:
                polling = self._clients.get(watch.forge)
                client = polling if isinstance(polling, ForgeClient) else None
                if client is not None and getattr(client, "authenticated", True) is False:
                    say(
                        "Watching %s by search: forge %r has no token, and notifications "
                        "need one (a classic PAT of %s's, in watch.users[].token)",
                        watch.user,
                        watch.forge,
                        watch.user,
                    )
                    watch.mode = "search"
                    return
        if client is None:
            say(
                "Watching %s by search: no %s client to read notifications with (forge %r)",
                watch.user,
                "token" if entry.token else "polling",
                watch.forge,
            )
            watch.mode = "search"
            return
        watch.client = client

        def give_up(reason: str) -> None:
            say("Watching %s by search: %s", watch.user, reason)
            watch.mode = "search"
            if watch.owns_client:
                client.close()
            watch.client = None
            watch.owns_client = False

        def not_yet(what: str) -> None:
            (say if first else logger.debug)(
                "Watching %s by search for now: %s; retrying each check",
                watch.user,
                what,
                exc_info=True,
            )

        # Notifications are always the token owner's inbox. Reading the polling
        # token's inbox as somebody else's would watch the wrong person.
        try:
            login = str(client.get_authenticated_user().get("login", ""))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403) and not _is_rate_limit_response(exc.response):
                # Refused, not unavailable: a dead token, or an app token that has
                # no user. Retrying a minute from now gets the same answer.
                give_up(
                    f"GET /user was refused (HTTP {exc.response.status_code}): the token is "
                    f"invalid or has no user (an app token), and notifications need a "
                    f"classic PAT of {watch.user}'s"
                )
            else:
                not_yet("could not tell whose token this is (GET /user failed)")
            return
        except Exception:
            not_yet("could not tell whose token this is (GET /user failed)")
            return
        if login.lower() != watch.user.lower():
            give_up(
                f"the token belongs to {login or 'nobody'!r}, and notifications are always "
                f"the token owner's. Give watch.users[].token a classic PAT of {watch.user}'s."
            )
            return
        since = timezone.now() - NOTIFICATION_LOOKBACK
        try:
            client.list_notifications(since)
        except NotificationsUnavailableError as exc:
            give_up(
                f"the token can't read notifications ({exc}). GitHub refuses them to "
                f"fine-grained and app tokens; a classic PAT with the notifications or "
                f"repo scope works."
            )
            return
        except NotImplementedError:
            give_up(f"forge {watch.forge!r} has no notifications API")
            return
        except Exception:
            not_yet("the notifications probe failed")
            return
        watch.mode = "notifications"
        watch.since = since

    # --- notifications -----------------------------------------------------------

    def _check_notifications(self, project_configs: Sequence[ProjectConfig]) -> None:
        by_name = {(pc.owner.lower(), pc.repo.lower()): pc for pc in project_configs}
        now = time.monotonic()
        for watch in self._users or []:
            if watch.mode != "notifications" or watch.client is None or now < watch.next_check:
                continue
            try:
                page = watch.client.list_notifications(watch.since)
            except NotificationsUnavailableError as exc:
                logger.warning(
                    "Notifications for %s stopped working (%s); watching by search from now on",
                    watch.user,
                    exc,
                )
                watch.mode = "search"
                continue
            except Exception:
                # Said once, at WARNING: the searches for this user run at the
                # backstop interval on the strength of these working.
                (logger.debug if watch.failing else logger.warning)(
                    "Notifications check for %s failed; retrying each check (the searches "
                    "still run every %d min)",
                    watch.user,
                    self.config.search_backstop_minutes,
                    exc_info=True,
                )
                watch.failing = True
                continue
            if watch.failing:
                watch.failing = False
                logger.info("Notifications for %s are being read again", watch.user)
            # The forge asks to be polled no more often than this; honour it.
            watch.next_check = now + max(0, page.poll_interval) - 1
            if not page.complete:
                # The notifications API pages newest first and filters only by
                # time, so the oldest of a burst can't be fetched without
                # re-reading the newest ones. The searches cover them.
                logger.info(
                    "More notifications for %s than one check reads; the oldest are left "
                    "to the searches (every %d min)",
                    watch.user,
                    self.config.search_backstop_minutes,
                )
            self._act_on_notifications(watch, page.items, by_name)

    def _act_on_notifications(
        self,
        watch: UserWatch,
        items: Sequence[dict[str, object]],
        by_name: Mapping[tuple[str, str], ProjectConfig],
    ) -> None:
        from franktheunicorn.backends.github import parse_github_timestamp
        from franktheunicorn.backends.poller import (
            RefreshInProgressError,
            refresh_notified_pull_request,
        )

        newest = watch.since
        # The oldest notification to retry: the watermark must not pass it, or the
        # retry the seen-pop below promises never comes.
        retry_from: datetime | None = None
        acted: list[str] = []

        def retry(thread: str, stamp: datetime | None) -> None:
            nonlocal retry_from
            watch.seen.pop(thread, None)
            if stamp is not None and (retry_from is None or stamp < retry_from):
                retry_from = stamp

        for item in items:
            thread = str(item.get("id", ""))
            updated = str(item.get("updated_at", ""))
            stamp = parse_github_timestamp(updated)
            if stamp is not None and (newest is None or stamp > newest):
                newest = stamp
            if not thread or watch.seen.get(thread) == updated:
                continue
            watch.seen[thread] = updated
            repository = item.get("repository")
            subject = item.get("subject")
            if not isinstance(repository, dict) or not isinstance(subject, dict):
                continue
            owner, _, repo = str(repository.get("full_name", "")).partition("/")
            pc = by_name.get((owner.lower(), repo.lower()))
            if pc is None or pc.forge != watch.forge:
                continue
            try:
                number = int(str(subject.get("url", "")).rstrip("/").rsplit("/", 1)[-1])
            except ValueError:
                continue
            if stamp is not None and _refreshed_since(pc, number, stamp):
                continue
            client = self._clients.get(pc.forge)
            if not isinstance(client, ForgeClient):
                continue
            try:
                pr = refresh_notified_pull_request(
                    client,
                    pc,
                    number,
                    self._oc.github_username,
                    repo_path=self._clone_path(pc),
                    merge_intent=self._oc.merge_intent,
                )
            except RefreshInProgressError:
                # Another thread is on it, but may have read the comments before
                # this activity landed. Not a failure: ask again next check.
                retry(thread, stamp)
                continue
            except Exception:
                tries = watch.failures.get(thread, 0) + 1
                if tries >= MAX_NOTIFICATION_RETRIES:
                    watch.failures.pop(thread, None)
                    logger.warning(
                        "Giving up on %s#%d after %d failed refreshes for a notification for "
                        "%s; the poll and the searches will still pick it up",
                        pc.full_name,
                        number,
                        tries,
                        watch.user,
                        exc_info=True,
                    )
                    continue
                watch.failures[thread] = tries
                logger.warning(
                    "Could not refresh %s#%d after a notification for %s; retrying next check",
                    pc.full_name,
                    number,
                    watch.user,
                    exc_info=True,
                )
                retry(thread, stamp)
                continue
            watch.failures.pop(thread, None)
            acted.append(f"{pc.full_name}#{number} ({item.get('reason', '?')})")
            if pr is not None:
                self._hand_off(pr, pc, client)
        if retry_from is not None and newest is not None:
            newest = min(newest, retry_from - timedelta(seconds=1))
        if newest is not None:
            watch.since = newest
            # Only entries the next listing can still return are worth keeping.
            watch.seen = {
                t: u
                for t, u in watch.seen.items()
                if (s := parse_github_timestamp(u)) is None or s >= newest
            }
        if acted:
            logger.info("Notifications for %s: refreshed %s", watch.user, ", ".join(acted))


def _merge(first: RecentSync, second: RecentSync) -> RecentSync:
    """Two walks from the same watermark: the second saw at least what the first did."""
    seen = {pr.pk for pr in [*second.new, *second.refreshed]}
    return RecentSync(
        closed=first.closed + second.closed,
        new=[*second.new, *(pr for pr in first.new if pr.pk not in seen)],
        refreshed=[*second.refreshed, *(pr for pr in first.refreshed if pr.pk not in seen)],
        deferred=second.deferred,
        in_flight=second.in_flight,
        newest=max((s for s in (first.newest, second.newest) if s is not None), default=None),
        resume_from=second.resume_from,
        complete=second.complete,
    )


def _refreshed_since(pc: ProjectConfig, number: int, stamp: datetime) -> bool:
    """Whether the row was fully refreshed after the notified activity."""
    last = (
        PullRequest.objects.filter(project__owner=pc.owner, project__repo=pc.repo, number=number)
        .values_list("last_polled_at", flat=True)
        .first()
    )
    return last is not None and last >= stamp
