"""The closed-PR follow-up scan behind the Zombie Queue.

A zombie is a pull request that was closed without being merged while the
JIRA ticket it was fixing stayed open. Projects the size of Spark grow them by
the hundred: a stale bot closes a PR after a quiet month, the author moves
on, and the ticket sits there looking unaddressed when a mostly-working fix
is one rebase away. This module finds them and asks the model two separate
questions — does the *ticket* still make sense, and did the *fix* — plus the
one that matters for reviving it: what would have to change to land it today.

It works backwards, newest closed PR first, because that is the order in which
a revival is likely to be cheap, and it is bounded per run and resumable: the
listing is a page at a time, the lowest PR number examined so far is the
cursor (``ZombieScanState``), and the LLM budget is spent newest-first on
whatever is still ``unassessed``, including candidates earlier runs found but
could not afford to ask about.

Three paths out:

* ``scan_project`` — the sweep, one project, from the dashboard button or the
  ``find_zombie_prs`` command (the worker's ``scan_zombie_prs`` runs it over
  every JIRA-enabled project).
* ``assess_pr`` — the per-PR re-run behind the detail page's button.
* ``zombie_projects`` — which projects the sweep can look at at all.

Nothing here is automatic. The poll cycle does not call it; a run costs GitHub
calls (one listing per 100 PRs, two per candidate), a JIRA fetch per candidate
and an LLM call per assessment, and that is somebody's decision to spend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from django.utils import timezone

from franktheunicorn.core.models import (
    ZOMBIE_QUEUE,
    Project,
    PullRequest,
    ZombieAssessment,
    ZombieScanState,
)
from franktheunicorn.data_access.jira.fetcher import JiraFetcher, extract_ticket_ids

if TYPE_CHECKING:
    from franktheunicorn.backends.base import ForgeClient
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig, ZombieQueueConfig
    from franktheunicorn.data_access.jira.types import JiraTicketResult
    from franktheunicorn.review.backends.base import BaseLLMBackend

logger = logging.getLogger(__name__)

#: ``CostRecord.action_type`` for the assessment call.
ACTION_TYPE = "zombie-assessment"

#: The verdicts the model may return. Anything else becomes "unclear" rather
#: than a guess at what it meant.
VERDICTS = frozenset({"revive", "revive-with-changes", "rethink", "drop", "unclear"})

_PAGE_SIZE = 100
#: PR numbers remembered for a JIRA retry across runs.
_MAX_JIRA_RETRY = 500
#: Keys at least one of which a real assessment carries. ``{}`` and
#: ``{"message": "unable to assess"}`` are JSON objects and not answers.
_ANSWER_KEYS = ("verdict", "jira_makes_sense", "fix_makes_sense", "needed_changes", "summary")
_BODY_CHARS = 3_000
_COMMENT_CHARS = 600

_SYSTEM_PROMPT = """You are helping the maintainers of {project} decide whether a pull request is worth reviving.

The PR was closed WITHOUT being merged, but the JIRA ticket it was addressing is still open. Judge two things separately:

1. Does the JIRA ticket still make sense as a change to make? (It may be obsolete, superseded, or a bad idea; or it may be a real gap that still needs fixing.)
2. Was the PR's fix the right approach? If it mostly was, say concretely what would need to change for it to land today: rebasing against moved code, missing tests, a reviewer's objection that was never answered, scope that should be trimmed.

Use the closing comments: a PR closed by a stale bot for inactivity is a different story from one a committer rejected. The code may have moved since the PR closed on {closed_at}; if the diff looks stale against what the ticket now needs, say so.

Answer with JSON only, no prose around it:
{{
  "jira_makes_sense": true | false | null,
  "jira_reasoning": "one or two sentences",
  "fix_makes_sense": true | false | null,
  "fix_reasoning": "one or two sentences",
  "verdict": "revive" | "revive-with-changes" | "rethink" | "drop" | "unclear",
  "needed_changes": ["each concrete change required to land this, if any"],
  "summary": "one or two sentences a maintainer can read in the queue",
  "confidence": 0.0-1.0
}}

Verdicts: "revive" — the fix is sound and only needs a rebase or trivial polish; "revive-with-changes" — the approach is right but the listed changes are needed; "rethink" — the ticket is valid but this PR is the wrong approach; "drop" — the ticket no longer makes sense or the fix is not worth salvaging; "unclear" — not enough to say. null for a question you cannot answer."""

_USER_PROMPT = """Project: {project}
PR #{number}: {title}
Author: {author}
URL: {url}
Closed at: {closed_at}

== JIRA ticket ==
{jira}

== Why the PR was closed (last comments, oldest first) ==
{closing_context}

== PR description ==
{body}

== PR diff{diff_note} ==
{diff}
"""


@dataclass
class ZombieScanRun:
    """What one ``scan_project`` call did, for the command log and the CLI."""

    project: str
    examined: int = 0
    merged: int = 0
    no_jira: int = 0
    jira_closed: int = 0
    jira_error: int = 0
    jira_retried: int = 0
    already_known: int = 0
    candidates: int = 0
    assessed: int = 0
    assessment_failed: int = 0
    pages: int = 0
    exhausted: bool = False
    error: str = ""
    skipped_reason: str = ""
    #: PRs this run added to the queue, for the CLI's listing.
    found: list[ZombieAssessment] = field(default_factory=list)
    #: PR numbers whose JIRA could not be reached, for the next run to retry.
    jira_retry: list[int] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"{self.project}: skipped — {self.skipped_reason}"
        parts = [
            f"{self.project}: examined {self.examined} closed PR(s) over {self.pages} page(s)",
            f"{self.candidates} new zombie(s)",
            f"{self.assessed} assessed",
        ]
        if self.assessment_failed:
            parts.append(f"{self.assessment_failed} assessment(s) failed")
        skipped = []
        if self.merged:
            skipped.append(f"{self.merged} merged")
        if self.no_jira:
            skipped.append(f"{self.no_jira} without a JIRA")
        if self.jira_closed:
            skipped.append(f"{self.jira_closed} JIRA closed")
        if self.jira_error:
            skipped.append(f"{self.jira_error} JIRA unreachable (retried next run)")
        if self.jira_retried:
            skipped.append(f"{self.jira_retried} retried from an earlier JIRA failure")
        if self.already_known:
            skipped.append(f"{self.already_known} already in the queue")
        if skipped:
            parts.append("skipped " + ", ".join(skipped))
        if self.exhausted:
            parts.append("reached the end of the closed-PR list")
        if self.error:
            parts.append(f"stopped early: {self.error}")
        return "; ".join(parts)


# --- Which projects, which backend -------------------------------------------


def zombie_gate_reason(project_config: ProjectConfig | None, project: str) -> str:
    """Why the scan can't look at *project*, or "" when it can.

    The one real gate is JIRA: without a server and a ticket prefix there is no
    way to tell an open ticket from a closed one, which is the whole question.
    """
    if project_config is None:
        return f"no project config for {project}"
    if not project_config.jira.enabled or not project_config.jira.server:
        return (
            f"{project}: jira.enabled / jira.server are not set in its project YAML, "
            "so there is no ticket to check"
        )
    return ""


def zombie_projects() -> list[tuple[Project, ProjectConfig]]:
    """Every enabled project the scan can run on, with its config.

    Creates the ``Project`` row when the YAML exists but the poller has not
    made one yet — the scan writes PR rows against it, and waiting for the
    first poll cycle to create it is a confusing reason for a button to do
    nothing.
    """
    from django.conf import settings

    from franktheunicorn.config.loader import load_project_configs

    result: list[tuple[Project, ProjectConfig]] = []
    for pc in load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", "")):
        if not pc.enabled or zombie_gate_reason(pc, pc.full_name):
            continue
        project, _created = Project.objects.get_or_create(
            owner=pc.owner,
            repo=pc.repo,
            defaults={"review_context": pc.review_context or ""},
        )
        if not project.enabled:
            continue
        result.append((project, pc))
    return result


def resolve_backend(operator_config: OperatorConfig) -> BaseLLMBackend | None:
    """The backend the assessment uses, or None when nothing is configured.

    ``zombie_queue.llm_backend`` first, then ``llm_backends`` in order, then
    resolved ``agent_cli_reviewers`` — security triage's fallthrough, borrowed
    rather than re-derived, so a deployment that gets triage gets this too.
    The first that builds as an LLM backend wins; there is no circuit breaker
    here because one run makes at most ``max_assessments_per_run`` calls.
    """
    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.backends.base import BaseLLMBackend
    from franktheunicorn.security.triage import _agent_cli_triage_configs

    configs = []
    if operator_config.zombie_queue.llm_backend is not None:
        configs.append(operator_config.zombie_queue.llm_backend)
    configs.extend(operator_config.llm_backends)
    configs.extend(_agent_cli_triage_configs(operator_config))
    for cfg in configs:
        try:
            backend = get_backend(cfg)
        except Exception:
            logger.warning(
                "Zombie assessment could not build backend %s/%s; trying the next",
                cfg.provider,
                cfg.model or "default model",
                exc_info=True,
            )
            continue
        if isinstance(backend, BaseLLMBackend):
            return backend
    return None


def _forge_client(project_config: ProjectConfig, operator_config: OperatorConfig) -> ForgeClient:
    from franktheunicorn.backends import make_client
    from franktheunicorn.config.resolver import get_forge_entry

    return make_client(get_forge_entry(operator_config, project_config.forge))


# --- The sweep ---------------------------------------------------------------


def scan_project(
    project: Project,
    operator_config: OperatorConfig,
    *,
    max_prs: int | None = None,
    max_assessments: int | None = None,
    restart: bool = False,
    forge_client: ForgeClient | None = None,
    backend: BaseLLMBackend | None = None,
) -> ZombieScanRun:
    """Walk *project*'s closed PRs from where the last run stopped, then assess.

    Two phases, deliberately in this order. The walk is cheap per PR — the
    listing payload carries ``merged_at``, the title and the body, so the only
    per-PR call is the JIRA fetch for closed-unmerged PRs that name a ticket —
    and it records every zombie it finds as ``unassessed``. The assessment
    phase then spends the LLM budget newest-first over everything unassessed
    in the project, so a run that found more than it could afford leaves the
    rest for the next run instead of forgetting them, and a backlog is always
    worked from the most recently opened PR down.

    *restart* throws the cursor away and begins again from the newest closed
    PR; PRs already in the queue are still skipped, so it does not duplicate.
    Never raises for a per-PR failure: those are counted and the run goes on.
    """
    from franktheunicorn.config.loader import get_project_config

    run = ZombieScanRun(project=project.full_name)
    project_config = get_project_config(project.full_name)
    reason = zombie_gate_reason(project_config, project.full_name)
    if reason or project_config is None:
        run.skipped_reason = reason
        logger.info("Zombie scan skipped: %s", reason)
        return run

    cfg = operator_config.zombie_queue
    budget_prs = max_prs if max_prs is not None else cfg.max_prs_per_run
    budget_llm = max_assessments if max_assessments is not None else cfg.max_assessments_per_run

    state, _ = ZombieScanState.objects.get_or_create(project=project)
    if restart:
        state.lowest_number_seen = None
        state.next_page = 1
        state.exhausted = False
        state.last_run_at = None

    # The watermark the next run's catch-up reads from is when *this* run
    # started listing, not when it finished: a PR closed during the minutes of
    # assessment below has an updated_at older than the finish time, and a
    # finish-time watermark would make the next catch-up stop before it.
    started_at = timezone.now()
    own_client = forge_client is None
    client = forge_client or _forge_client(project_config, operator_config)
    try:
        with httpx.Client(timeout=30.0) as jira_http:
            jira = JiraFetcher(client=jira_http)
            walker = _Walker(run, state, project, project_config, cfg, client, jira, budget_prs)
            walker.retry_jira_failures()
            walker.catch_up()
            walker.walk_history()
        if backend is None:
            backend = resolve_backend(operator_config)
        if backend is None:
            logger.warning(
                "Zombie scan for %s found %d zombie(s) but has no LLM backend to assess them: "
                "set zombie_queue.llm_backend or llm_backends in operator.yaml. They are in "
                "the queue as 'not yet assessed'.",
                project.full_name,
                run.candidates,
            )
        else:
            _assess_backlog(run, project, project_config, cfg, client, backend, budget_llm)
    finally:
        if own_client:
            client.close()

    state.prs_examined += run.examined
    state.candidates_found += run.candidates
    state.last_run_at = started_at
    # PRs whose JIRA could not be reached this run — including retries that
    # failed again. Capped so an outage during a long walk cannot grow it
    # without bound; the oldest are the ones dropped.
    state.jira_retry = sorted(set(run.jira_retry))[-_MAX_JIRA_RETRY:]
    state.last_summary = run.summary()
    state.save()
    logger.info("Zombie scan: %s", run.summary())
    return run


class _Walker:
    """The two listing passes over one project's closed PRs, sharing a budget.

    ``catch_up`` reads the closed list newest-*updated* first and stops at the
    first PR not touched since the last run: a close bumps ``updated_at``, so
    this is every PR closed (or reopened and closed) since then, whatever its
    number — including old PRs a stale bot shut last night, which the history
    walk passed months ago. ``walk_history`` reads newest-*created* first from
    the stored page, skipping numbers at or above the frontier, which is how
    it resumes. Both are bounded by the same examined-PR budget; the catch-up
    goes first because what just closed is what is cheapest to revive.
    """

    def __init__(
        self,
        run: ZombieScanRun,
        state: ZombieScanState,
        project: Project,
        project_config: ProjectConfig,
        cfg: ZombieQueueConfig,
        client: ForgeClient,
        jira: JiraFetcher,
        budget_prs: int,
    ) -> None:
        self.run = run
        self.state = state
        self.project = project
        self.project_config = project_config
        self.cfg = cfg
        self.client = client
        self.jira = jira
        self.budget_prs = budget_prs
        self.closed_statuses = {s.strip().lower() for s in cfg.jira_closed_statuses}
        # Pages one run may read, over both passes. The history walk can turn
        # whole pages over without examining anything when the list has
        # shifted, and this is what stops that from becoming a crawl of the
        # entire closed list.
        self.page_budget = budget_prs // _PAGE_SIZE + 4

    def _page(self, page: int, sort: str) -> list[dict[str, Any]] | None:
        """One listing page, or None when the pass has to stop."""
        if self.run.pages >= self.page_budget:
            return None
        try:
            batch = self.client.list_pull_requests_page(
                self.project.owner,
                self.project.repo,
                state="closed",
                page=page,
                per_page=_PAGE_SIZE,
                sort=sort,
            )
        except NotImplementedError:
            self.run.error = f"{type(self.client).__name__} cannot page through closed PRs"
            logger.warning("Zombie scan for %s: %s", self.project.full_name, self.run.error)
            return None
        except Exception as exc:
            self.run.error = (
                f"listing closed PRs (page {page}, by {sort}) failed: {type(exc).__name__}: {exc}"
            )
            logger.warning("Zombie scan for %s: %s", self.project.full_name, self.run.error)
            return None
        self.run.pages += 1
        return batch

    def _examine(self, pr_data: dict[str, Any]) -> None:
        self.run.examined += 1
        _examine(
            self.run,
            self.project,
            self.project_config,
            self.cfg,
            self.jira,
            self.closed_statuses,
            pr_data,
        )

    def retry_jira_failures(self) -> None:
        """Re-examine the PRs an earlier run could not check against JIRA.

        The frontier moved past them when the fetch failed, so without this
        a JIRA outage during a walk silently dropped every zombie in it. One
        PR fetch each — the listing payload is gone — against the shared
        examined budget. Whatever fails again is kept for the run after.
        """
        for number in list(self.state.jira_retry):
            if self.run.examined >= self.budget_prs:
                # Out of budget: carry the rest forward untouched.
                self.run.jira_retry.append(number)
                continue
            try:
                pr_data = self.client.get_pull_request(
                    self.project.owner, self.project.repo, number
                )
            except Exception:
                logger.debug("Zombie scan: retry fetch of PR #%d failed", number, exc_info=True)
                self.run.jira_retry.append(number)
                continue
            if pr_data.get("state") != "closed":
                # Reopened since: the poller's problem now, not a zombie.
                continue
            self.run.jira_retried += 1
            self._examine(pr_data)

    def catch_up(self) -> None:
        since = self.state.last_run_at
        if since is None:
            # First run: the history walk starts at the top anyway.
            return
        page = 1
        while self.run.examined < self.budget_prs:
            batch = self._page(page, "updated")
            if not batch:
                return
            for pr_data in batch:
                if not isinstance(pr_data.get("number"), int):
                    continue
                updated = _parse_datetime(pr_data.get("updated_at"))
                if updated is not None and updated <= since:
                    return
                self._examine(pr_data)
                if self.run.examined >= self.budget_prs:
                    return
            if len(batch) < _PAGE_SIZE:
                return
            page += 1

    def walk_history(self) -> None:
        state = self.state
        if state.exhausted:
            # Every PR closed before the last full pass has been examined;
            # the catch-up covers everything since.
            self.run.exhausted = True
            return
        frontier = state.lowest_number_seen
        page = state.next_page
        stopped_early = False

        while self.run.examined < self.budget_prs:
            batch = self._page(page, "created")
            if batch is None:
                stopped_early = True
                break
            if not batch:
                self.run.exhausted = True
                break
            whole_page = True
            for pr_data in batch:
                number = pr_data.get("number")
                if not isinstance(number, int):
                    continue
                # The list shifted since the page number was stored: these were
                # examined by an earlier run.
                if frontier is not None and number >= frontier:
                    continue
                if self.run.examined >= self.budget_prs:
                    # Budget spent mid-page: stay on this page, the frontier
                    # skips the part of it already examined.
                    whole_page = False
                    break
                frontier = number
                self._examine(pr_data)
            if whole_page and len(batch) < _PAGE_SIZE:
                self.run.exhausted = True
                break
            if whole_page:
                page += 1
            if self.run.examined >= self.budget_prs:
                break

        state.lowest_number_seen = frontier
        if not stopped_early:
            state.next_page = page
        state.exhausted = self.run.exhausted


def _examine(
    run: ZombieScanRun,
    project: Project,
    project_config: ProjectConfig,
    cfg: ZombieQueueConfig,
    jira: JiraFetcher,
    closed_statuses: set[str],
    pr_data: dict[str, Any],
) -> None:
    """One closed PR: is it a zombie, and if so record it."""
    number = pr_data["number"]
    if pr_data.get("merged_at"):
        run.merged += 1
        return
    text = f"{pr_data.get('title', '')} {pr_data.get('body') or ''}"
    tickets = extract_ticket_ids(text, project_config.jira.project_prefix)
    if not tickets:
        run.no_jira += 1
        return
    if ZombieAssessment.objects.filter(
        pull_request__project=project, pull_request__number=number
    ).exists():
        run.already_known += 1
        return
    ticket_id = tickets[0]
    try:
        ticket = jira.fetch(project_config.jira.server, ticket_id)
    except Exception as exc:
        run.jira_error += 1
        run.jira_retry.append(number)
        logger.debug("Zombie scan: JIRA %s for PR #%d failed: %s", ticket_id, number, exc)
        return
    if ticket.status.strip().lower() in closed_statuses:
        run.jira_closed += 1
        return

    assessment = _record_zombie(project, project_config, pr_data, ticket)
    run.candidates += 1
    run.found.append(assessment)
    logger.info(
        "Zombie found: %s #%d (%s, JIRA %s is %s)",
        project.full_name,
        number,
        pr_data.get("title", "")[:80],
        ticket_id,
        ticket.status or "open",
    )


def _record_zombie(
    project: Project,
    project_config: ProjectConfig,
    pr_data: dict[str, Any],
    ticket: JiraTicketResult,
) -> ZombieAssessment:
    """Upsert the PR row into the zombie queue and open its assessment."""
    from franktheunicorn.backends.poller import _upsert_pull_request

    pr = _upsert_pull_request(project, pr_data, None)
    pr.queue = ZOMBIE_QUEUE
    pr.jira_ticket_id = ticket.ticket_id
    pr.jira_cache = ticket.to_cache_dict()
    pr.save(update_fields=["queue", "jira_ticket_id", "jira_cache", "updated_at"])
    return ZombieAssessment.objects.create(
        pull_request=pr,
        jira_ticket_id=ticket.ticket_id,
        jira_status=ticket.status[:100],
        jira_summary=ticket.summary[:1000],
        jira_url=f"{project_config.jira.server}/browse/{ticket.ticket_id}",
        pr_closed_at=_parse_datetime(pr_data.get("closed_at")),
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- The assessment ----------------------------------------------------------


def _assess_backlog(
    run: ZombieScanRun,
    project: Project,
    project_config: ProjectConfig,
    cfg: ZombieQueueConfig,
    client: ForgeClient,
    backend: BaseLLMBackend,
    budget_llm: int,
) -> None:
    """Spend the LLM budget on the project's unassessed zombies, newest PR first.

    Includes what earlier runs found and could not afford. Ones that have
    failed ``MAX_ATTEMPTS`` times are left alone — a backend that cannot
    produce JSON for a PR will not do better on the fourth try, and retrying
    it every run would starve the rest.
    """
    backlog = (
        ZombieAssessment.objects.filter(
            pull_request__project=project,
            verdict="unassessed",
            disposition="pending",
            attempts__lt=ZombieAssessment.MAX_ATTEMPTS,
        )
        .select_related("pull_request")
        .order_by("-pull_request__number")[:budget_llm]
    )
    for assessment in backlog:
        if assess(assessment, project_config, cfg, client, backend):
            run.assessed += 1
        else:
            run.assessment_failed += 1


def assess(
    assessment: ZombieAssessment,
    project_config: ProjectConfig,
    cfg: ZombieQueueConfig,
    client: ForgeClient,
    backend: BaseLLMBackend,
) -> bool:
    """Ask the model about one zombie and write its answer. True on a verdict.

    A failure — the backend raised, or answered in prose — leaves the verdict
    where it was, bumps ``attempts`` and keeps the reason in ``last_error`` so
    the queue can show "assessment failed: ..." instead of silently staying
    "not yet assessed".
    """
    pr = assessment.pull_request
    owner, repo = project_config.owner, project_config.repo

    assessment.closing_context = _closing_context(client, owner, repo, pr.number, cfg.max_comments)
    diff = ""
    try:
        diff = client.get_pull_request_diff(owner, repo, pr.number)
    except Exception:
        logger.debug("Zombie assessment: diff fetch failed for PR #%d", pr.number, exc_info=True)
    diff_note = ""
    if not diff:
        diff = "(diff unavailable — judge from the description and the ticket)"
    elif len(diff) > cfg.max_diff_chars:
        diff = diff[: cfg.max_diff_chars] + "\n... (diff truncated)"
        diff_note = ", truncated"

    closed_at = assessment.pr_closed_at.date().isoformat() if assessment.pr_closed_at else "?"
    system = _SYSTEM_PROMPT.format(project=project_config.full_name, closed_at=closed_at)
    user = _USER_PROMPT.format(
        project=project_config.full_name,
        number=pr.number,
        title=pr.title,
        author=pr.author,
        url=pr.url,
        closed_at=closed_at,
        jira=_jira_text(pr, assessment),
        closing_context=assessment.closing_context or "(no comments)",
        body=(pr.body or "(no description)")[:_BODY_CHARS],
        diff_note=diff_note,
        diff=diff,
    )

    assessment.attempts += 1
    assessment.backend_used = backend.label
    try:
        raw = backend.metered_call(
            system,
            user,
            action_type=ACTION_TYPE,
            project_id=pr.project_id,
            pr_id=pr.pk,
        )
    except Exception as exc:
        assessment.last_error = f"{backend.label}: {type(exc).__name__}: {exc}"[:500]
        assessment.save()
        logger.warning(
            "Zombie assessment of PR #%d failed (attempt %d): %s",
            pr.number,
            assessment.attempts,
            assessment.last_error,
        )
        return False

    from franktheunicorn.security.triage import _safe_json_parse

    data = _safe_json_parse(raw)
    if data is None or not any(key in data for key in _ANSWER_KEYS):
        # ``{}`` and ``{"message": "unable to assess"}`` parse fine and would
        # otherwise be filed as a confident "unclear" and leave the backlog.
        what = "a JSON object" if data is None else "an assessment in its JSON"
        assessment.last_error = f"{backend.label} answered without {what}"[:500]
        assessment.save()
        logger.warning(
            "Zombie assessment of PR #%d: %s (attempt %d)",
            pr.number,
            assessment.last_error,
            assessment.attempts,
        )
        return False

    _apply_answer(assessment, data)
    assessment.last_error = ""
    assessment.assessed_at = timezone.now()
    assessment.save()
    logger.info(
        "Zombie assessment of PR #%d: %s (%s)",
        pr.number,
        assessment.verdict,
        assessment.summary[:120],
    )
    return True


def _apply_answer(assessment: ZombieAssessment, data: dict[str, object]) -> None:
    from franktheunicorn.security.triage import _coerce_tristate

    verdict = str(data.get("verdict", "")).strip().lower()
    assessment.verdict = verdict if verdict in VERDICTS else "unclear"
    assessment.jira_makes_sense = _coerce_tristate(data.get("jira_makes_sense"))
    assessment.fix_makes_sense = _coerce_tristate(data.get("fix_makes_sense"))
    assessment.jira_reasoning = str(data.get("jira_reasoning") or "")[:4000]
    assessment.fix_reasoning = str(data.get("fix_reasoning") or "")[:4000]
    assessment.summary = str(data.get("summary") or "")[:4000]
    changes = data.get("needed_changes")
    if isinstance(changes, list):
        assessment.needed_changes = [str(c)[:1000] for c in changes if str(c).strip()][:20]
    elif isinstance(changes, str) and changes.strip():
        assessment.needed_changes = [changes.strip()[:1000]]
    else:
        assessment.needed_changes = []
    confidence = data.get("confidence")
    if isinstance(confidence, int | float) and not isinstance(confidence, bool):
        assessment.confidence = max(0.0, min(1.0, float(confidence)))
    else:
        assessment.confidence = None


def _closing_context(
    client: ForgeClient, owner: str, repo: str, number: int, max_comments: int
) -> str:
    try:
        comments = client.get_issue_comments(owner, repo, number)
    except Exception:
        logger.debug("Zombie assessment: comments fetch failed for PR #%d", number, exc_info=True)
        return ""
    lines = []
    for comment in comments[-max_comments:]:
        user = comment.get("user") or {}
        author = user.get("login", "?") if isinstance(user, dict) else "?"
        when = str(comment.get("created_at", ""))[:10]
        body = str(comment.get("body") or "").strip().replace("\r", "")
        if len(body) > _COMMENT_CHARS:
            body = body[:_COMMENT_CHARS] + "..."
        lines.append(f"[{author} {when}] {body}")
    return "\n".join(lines)


def _jira_text(pr: PullRequest, assessment: ZombieAssessment) -> str:
    cache = pr.jira_cache if isinstance(pr.jira_cache, dict) else {}
    parts = [
        f"JIRA {assessment.jira_ticket_id}: {cache.get('summary') or assessment.jira_summary}",
        f"Status: {cache.get('status') or assessment.jira_status or '?'}"
        f" | Priority: {cache.get('priority') or '?'}"
        f" | Assignee: {cache.get('assignee') or 'unassigned'}",
    ]
    description = str(cache.get("description") or "")
    if description:
        parts.append(f"Description: {description[:1500]}")
    comments = cache.get("recent_comments")
    if isinstance(comments, list) and comments:
        parts.append("Recent comments:")
        for c in comments[:5]:
            if isinstance(c, dict):
                parts.append(f"  [{c.get('author', '?')}] {str(c.get('body') or '')[:300]}")
    return "\n".join(parts)


# --- The per-PR re-run --------------------------------------------------------


def assess_pr(pr: PullRequest, operator_config: OperatorConfig) -> str:
    """Re-run (or first-run) the assessment on one PR. Returns a one-line outcome.

    The detail page's button. Raises when nothing can be done — no project
    config, no backend — and when the model's answer was unusable, so the
    worker command fails with the reason in front of the operator rather
    than completing with the old verdict still on the page as if refreshed.
    """
    from franktheunicorn.config.loader import get_project_config

    project_config = get_project_config(pr.project.full_name)
    reason = zombie_gate_reason(project_config, pr.project.full_name)
    if reason or project_config is None:
        raise ValueError(reason)
    backend = resolve_backend(operator_config)
    if backend is None:
        msg = (
            "no LLM backend configured for the zombie assessment: set zombie_queue.llm_backend "
            "or llm_backends in operator.yaml"
        )
        raise ValueError(msg)

    assessment, created = ZombieAssessment.objects.get_or_create(
        pull_request=pr,
        defaults={
            "jira_ticket_id": pr.jira_ticket_id or "",
            "jira_url": (
                f"{project_config.jira.server}/browse/{pr.jira_ticket_id}"
                if pr.jira_ticket_id
                else ""
            ),
        },
    )
    if created and pr.queue != ZOMBIE_QUEUE:
        pr.queue = ZOMBIE_QUEUE
        pr.save(update_fields=["queue", "updated_at"])
    # A fresh verdict is a fresh count: the attempts cap guards the sweep's
    # retries, not the operator's.
    assessment.attempts = 0
    client = _forge_client(project_config, operator_config)
    try:
        ok = assess(assessment, project_config, operator_config.zombie_queue, client, backend)
    finally:
        client.close()
    if not ok:
        msg = f"assessment failed: {assessment.last_error}"
        raise RuntimeError(msg)
    return f"verdict={assessment.verdict}: {assessment.summary[:200]}"
