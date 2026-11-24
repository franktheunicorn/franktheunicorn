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
it settled on. A user read through notifications is still searched, less often
(``watch.search_backstop_minutes``): notifications say what moved, the searches
say what the whole set is.
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

from django.db import connection
from django.utils import timezone

from franktheunicorn.backends.base import ForgeClient, NotificationsUnavailableError
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


@dataclass
class ReviewHandoff:
    """A PR the watcher refreshed that the cycle should consider reviewing."""

    pr: PullRequest
    pc: ProjectConfig
    client: object
    own: bool


@dataclass
class UserWatch:
    """How one watched user's involvement is being found, after the startup probe."""

    user: str
    forge: str
    mode: str  # "notifications" or "search"
    client: ForgeClient | None = None
    owns_client: bool = False
    since: datetime | None = None
    next_check: float = 0.0
    #: Notification thread id → the updated_at last acted on.
    seen: dict[str, str] = field(default_factory=dict)


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
            if watch.forge == forge and watch.user.lower() == user.lower():
                return watch.mode == "notifications"
        return False

    def covers(self, client: object) -> bool:
        """Whether this watcher reads *client*'s projects (so the mid-cycle
        full re-listing can leave them alone)."""
        return getattr(client, "supports_updated_since", False) is True

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

    def _check_projects(self, project_configs: Sequence[ProjectConfig]) -> None:
        from franktheunicorn.backends.poller import sync_recent_pull_requests

        watched = [w.user for w in self._users or []]
        for pc in project_configs:
            client = self._clients[pc.forge]
            if not self.covers(client):
                continue
            assert isinstance(client, ForgeClient)
            since = self._watermarks.get(pc.full_name)
            if since is None:
                self._establish_watermark(pc, client)
                continue
            try:
                result = sync_recent_pull_requests(
                    pc,
                    client,
                    self._oc.github_username,
                    since,
                    watched_users=watched,
                    repo_path=self._clone_path(pc),
                    merge_intent=self._oc.merge_intent,
                    concurrency=max(1, self._oc.worker_pool.refresh_concurrency),
                    max_refreshes=self.config.max_refreshes_per_check,
                )
            except Exception:
                logger.warning(
                    "Watcher could not read %s's changes; it tries again next check",
                    pc.full_name,
                    exc_info=True,
                )
                continue
            if result.newest is not None:
                self._watermarks[pc.full_name] = max(since, result.newest - WATERMARK_OVERLAP)
            if not result.complete:
                logger.info(
                    "%s: more PRs moved since %s than one check reads; the rest are the "
                    "cycle's poll's",
                    pc.full_name,
                    since.isoformat(timespec="seconds"),
                )
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

    def _establish_watermark(self, pc: ProjectConfig, client: ForgeClient) -> None:
        """First sight of a project: note where "now" is, act on nothing.

        The cycle starts with a full poll of every project, so acting on the
        first page here would only race it for the same PRs.
        """
        from franktheunicorn.backends.github import parse_github_timestamp

        try:
            listing = client.list_pull_requests_updated_since(pc.owner, pc.repo, None)
        except Exception:
            logger.debug("Watcher could not list %s yet", pc.full_name, exc_info=True)
            return
        stamps = [parse_github_timestamp(i.get("updated_at")) for i in listing.items]
        newest = max((s for s in stamps if s is not None), default=None)
        self._watermarks[pc.full_name] = (newest or timezone.now()) - WATERMARK_OVERLAP
        logger.debug("Watcher watching %s from %s", pc.full_name, self._watermarks[pc.full_name])

    def _hand_off(self, pr: PullRequest, pc: ProjectConfig, client: object) -> None:
        with self._lock:
            self._handoff.append(
                ReviewHandoff(pr, pc, client, own=bool(getattr(pr, "is_operator_pr", False)))
            )

    # --- users -----------------------------------------------------------------

    def _resolve_users(self) -> list[UserWatch]:
        """Probe each watched user once: notifications if configured and possible."""
        resolved: list[UserWatch] = []
        for entry in self.config.users_for(self._oc.github_username):
            watch = UserWatch(entry.user, entry.forge, "search")
            if entry.source != "search":
                self._try_notifications(entry, watch)
            resolved.append(watch)
        if resolved:
            logger.info(
                "Watching %s",
                "; ".join(
                    f"{w.user} via {w.mode}"
                    + (" (searches as a backstop)" if w.mode == "notifications" else "")
                    for w in resolved
                ),
            )
        return resolved

    def _try_notifications(self, entry: WatchedUserConfig, watch: UserWatch) -> None:
        explicit = entry.source == "notifications"
        say = logger.warning if explicit else logger.info

        client: ForgeClient | None = None
        owns = False
        if entry.token:
            client = self._client_for_token(entry) if self._client_for_token else None
            owns = client is not None
        else:
            polling = self._clients.get(entry.forge)
            client = polling if isinstance(polling, ForgeClient) else None
        if client is None:
            say(
                "Watching %s by search: no %s client to read notifications with (forge %r)",
                entry.user,
                "token" if entry.token else "polling",
                entry.forge,
            )
            return

        def give_up(reason: str) -> None:
            say("Watching %s by search: %s", entry.user, reason)
            if owns:
                client.close()

        # Notifications are always the token owner's inbox. Reading the polling
        # token's inbox as somebody else's would watch the wrong person.
        try:
            login = str(client.get_authenticated_user().get("login", ""))
        except Exception:
            give_up("could not tell whose token this is (GET /user failed)")
            return
        if login.lower() != entry.user.lower():
            give_up(
                f"the token belongs to {login or 'nobody'!r}, and notifications are always "
                f"the token owner's. Give watch.users[].token a classic PAT of {entry.user}'s."
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
            give_up(f"forge {entry.forge!r} has no notifications API")
            return
        except Exception:
            # Transient: keep notifications and let the first real check retry.
            logger.debug("Notifications probe for %s failed", entry.user, exc_info=True)
        watch.mode = "notifications"
        watch.client = client
        watch.owns_client = owns
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
                logger.debug("Notifications check for %s failed", watch.user, exc_info=True)
                continue
            # The forge asks to be polled no more often than this; honour it.
            watch.next_check = now + max(0, page.poll_interval) - 1
            self._act_on_notifications(watch, page.items, by_name)

    def _act_on_notifications(
        self,
        watch: UserWatch,
        items: Sequence[dict[str, object]],
        by_name: Mapping[tuple[str, str], ProjectConfig],
    ) -> None:
        from franktheunicorn.backends.github import parse_github_timestamp
        from franktheunicorn.backends.poller import refresh_notified_pull_request

        newest = watch.since
        # The oldest notification whose refresh failed: the watermark must not
        # pass it, or the retry the seen-pop below promises never comes.
        retry_from: datetime | None = None
        acted: list[str] = []
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
            except Exception:
                logger.warning(
                    "Could not refresh %s#%d after a notification for %s",
                    pc.full_name,
                    number,
                    watch.user,
                    exc_info=True,
                )
                watch.seen.pop(thread, None)  # try again next check
                if stamp is not None and (retry_from is None or stamp < retry_from):
                    retry_from = stamp
                continue
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


def _refreshed_since(pc: ProjectConfig, number: int, stamp: datetime) -> bool:
    """Whether the row was fully refreshed after the notified activity."""
    last = (
        PullRequest.objects.filter(project__owner=pc.owner, project__repo=pc.repo, number=number)
        .values_list("last_polled_at", flat=True)
        .first()
    )
    return last is not None and last >= stamp
