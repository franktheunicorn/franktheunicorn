"""Learn each project's committers from the forge, once, at worker boot.

Two sources on the project config, in order:

* ``committer_teams`` — ``org/team-slug`` (``apache/spark-committers``): the
  team's members. The precise answer, readable by any org member's token
  (classic tokens need ``read:org``).
* ``committer_repos`` — ``owner/repo`` (``apache/spark``, or a private repo
  only committers can see): everyone with write access. Needs a write-access
  token, and counts the org's owners too, so it is read only when no team
  could be (or none is configured).

The union of whichever tier answered is stored on the Project row, where
core.committers.project_committers folds it in with the config's
hand-written ``committers``. Once per boot: committer votes are rare, and a
restart is how a new one gets picked up.

A source that cannot be read is logged at WARNING with what would fix it;
the others still count. If nothing can be read the stored list stays, so an
expired token does not strip every committer's star.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from django.utils import timezone

from franktheunicorn.backends.base import ForgeClient
from franktheunicorn.config.models import OperatorConfig, ProjectConfig

logger = logging.getLogger(__name__)


@dataclass
class CommitterSync:
    """One project's boot read: what answered, what did not, what was stored."""

    project: str
    logins: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    stored: bool = False


def _why(exc: Exception, source: str, *, team: bool) -> str:
    if isinstance(exc, NotImplementedError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401:
            return "401: the forge token was refused"
        if team and code in (403, 404):
            return (
                f"{code}: team {source} is not visible to this token "
                "(it needs to belong to an org member and, if classic, have read:org)"
            )
        if code == 403:
            return f"403: listing {source}'s collaborators needs a token with write access to it"
        if code == 404:
            return f"404: {source} does not exist or this token cannot see it"
        return f"HTTP {code}"
    return f"{type(exc).__name__}: {exc}"


def _read_tier(
    entries: Sequence[str],
    fetch: Callable[[str, str], list[str]],
    result: CommitterSync,
    *,
    team: bool,
) -> list[str]:
    """Read every entry of one tier; returns the logins of those that answered."""
    logins: list[str] = []
    for entry in entries:
        first, _, second = entry.partition("/")
        try:
            found = fetch(first, second)
        except Exception as exc:
            result.failed[entry] = _why(exc, entry, team=team)
            continue
        result.read.append(entry)
        logins.extend(found)
    return logins


def sync_project_committers(client: ForgeClient, project_config: ProjectConfig) -> CommitterSync:
    """Read one project's committer sources and store the union on its row."""
    from franktheunicorn.backends.poller import _project_row

    name = f"{project_config.owner}/{project_config.repo}"
    result = CommitterSync(project=name)
    found = _read_tier(project_config.committer_teams, client.list_team_members, result, team=True)
    if not result.read and project_config.committer_repos:
        found = _read_tier(
            project_config.committer_repos,
            lambda owner, repo: client.list_repo_collaborators(owner, repo, permission="push"),
            result,
            team=False,
        )

    seen: set[str] = set()
    for login in found:
        key = login.lower()
        if key not in seen and not key.endswith("[bot]"):
            seen.add(key)
            result.logins.append(login)

    for entry, why in result.failed.items():
        logger.warning(
            "%s: could not read committers from %s (%s); %s",
            name,
            entry,
            why,
            f"using {', '.join(result.read)}"
            if result.read
            else "keeping the committers stored at the last successful boot",
        )
    if not result.read:
        return result

    project = _project_row(project_config)
    project.committers_cache = sorted(result.logins, key=str.lower)
    project.committers_cache_at = timezone.now()
    project.save(update_fields=["committers_cache", "committers_cache_at", "updated_at"])
    result.stored = True
    logger.info("%s: %d committer(s) from %s", name, len(result.logins), ", ".join(result.read))
    return result


def committer_clients(
    operator_config: OperatorConfig, clients: Mapping[str, ForgeClient]
) -> dict[str, ForgeClient]:
    """The clients to read committers with: the polling ones, except where a
    forge entry sets ``committers_token`` — then a client on that token.

    Team membership needs an org member's token and the collaborator list a
    write-access one; the polling token is often an app/bot token that is
    neither. A GitHub client built here gets no shared rate limiter: that
    tracks the *polling* token's quota from response headers, and this token's
    headers would overwrite it (the watcher's notification clients, same).
    """
    from franktheunicorn.backends import make_client
    from franktheunicorn.config.resolver import get_forge_entry

    out = dict(clients)
    for name in clients:
        try:
            entry = get_forge_entry(operator_config, name)
        except KeyError:
            continue
        if not entry.committers_token or entry.committers_token == entry.token:
            continue
        try:
            if entry.type == "github":
                from franktheunicorn.backends.github import GitHubClient
                from franktheunicorn.data_access.base import GITHUB_API_BASE

                out[name] = GitHubClient(
                    token=entry.committers_token, base_url=entry.base_url or GITHUB_API_BASE
                )
            else:
                out[name] = make_client(entry.model_copy(update={"token": entry.committers_token}))
        except (NotImplementedError, ValueError) as exc:
            logger.warning(
                "forge %r: could not build a client on committers_token (%s); "
                "reading committers with the polling token",
                name,
                exc,
            )
            continue
        logger.info("forge %r: reading committers with committers_token", name)
    return out


def sync_committers_at_boot(
    clients: Mapping[str, ForgeClient], project_configs: Sequence[object]
) -> list[CommitterSync]:
    """``sync_project_committers`` for every enabled project that names a source."""
    results: list[CommitterSync] = []
    for pc in project_configs:
        if not isinstance(pc, ProjectConfig) or not pc.enabled:
            continue
        if not pc.committer_teams and not pc.committer_repos:
            continue
        client = clients.get(pc.forge)
        if client is None:
            logger.warning(
                "%s/%s: no client for forge %r, so its committer teams/repos were not read",
                pc.owner,
                pc.repo,
                pc.forge,
            )
            continue
        try:
            results.append(sync_project_committers(client, pc))
        except Exception:
            # Boot must not die on this: the stars are cosmetic next to polling.
            logger.exception("%s/%s: committer sync failed", pc.owner, pc.repo)
    return results
