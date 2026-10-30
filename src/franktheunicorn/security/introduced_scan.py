"""Scheduled introduced-CVE scan: did recent changes introduce a new CVE?

A per-project, cadenced check-in (daily / weekly / monthly). On daily it walks
the last ``lookback_days`` of commits on the default branch; on weekly and
monthly it diffs from the last final release tag to HEAD. A coding agent reads
the diff plus the project's previously accepted and previously rejected CVE
reports as context (so it doesn't re-report what the operator already ruled
on) and replies with the findings it thinks are newly introduced CVEs. Each
finding is filed as a :class:`~franktheunicorn.core.models.SecurityReport` with
``source="introduced-scan"`` (a separate queue for triaging), and the scan
then queues exploit verification on each — the "launch its own instance of the
project and verify it's really exploitable" step.

Read-only like the patch evaluator: the agent reads the diff against the
surrounding code, it doesn't run anything. The verification step (verify_exploit)
is the one that runs code, and it's a separate, already-existing command.
Borrows the verifier's reviewer and checkout helpers, like the siblings. Never
raises: every early return carries a reason.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from franktheunicorn.core.models import IntroducedScanRun, SecurityReport
from franktheunicorn.magpie.skills import skills_block_for_project
from franktheunicorn.security.introduced_scan_prompt import (
    ADDENDUM_TEMPLATE,
)
from franktheunicorn.security.introduced_scan_prompt import (
    TEMPLATE as _PROMPT_TEMPLATE,
)
from franktheunicorn.security.review_guidance import resolve_review_guidance
from franktheunicorn.security.verifier import (
    _checkout,
    _default_branch,
    _local_checkout,
    refresh_from_upstream,
    resolve_verifier_reviewer,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import (
        AgentCLIReviewerConfig,
        IntroducedScanConfig,
        OperatorConfig,
        ProjectConfig,
        SecurityDuplicateConfig,
    )
    from franktheunicorn.core.models import IntroducedBug, Project
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

#: Cap the diff in the prompt. A day of Spark is large; a week is larger. The
#: agent gets the most recent part and a note that it was truncated.
_MAX_DIFF_CHARS = 200_000

#: Linux caps a single argv string at 128 KB (MAX_ARG_STRLEN). A Spark
#: introduced-scan prompt (diff + CVE context + guidance) blows past that, so
#: prompts over this size go to the agent via stdin instead of ``-p <prompt>``.
_MAX_ARG_PROMPT_CHARS = 100_000

#: How much of each prior CVE report's text to include as context.
_MAX_CONTEXT_CHARS = 4_000

#: Cadence → minimum time between runs. The scheduler reads this.
CADENCE_INTERVAL: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
    # Monthly, but on the calendar's last day (is_due); the day keeps it to one run.
    "month-end": timedelta(days=1),
}


def _month_end_due(project: Project, now: datetime) -> bool:
    """On the month's last day, or later if the last month-end went unscanned."""
    today = timezone.localtime(now).date()
    if (today + timedelta(days=1)).month != today.month:
        return True
    previous_end = today.replace(day=1) - timedelta(days=1)
    last = IntroducedScanRun.objects.filter(project=project).order_by("-started_at").first()
    return last is not None and timezone.localtime(last.started_at).date() < previous_end


@dataclass
class IntroducedScanResult:
    """The outcome of one scan run for one project."""

    #: A gate-decline reason. Empty is the good case.
    error: str = ""
    cadence: str = "daily"
    #: "last 2 days" or "v3.5.0..master".
    commit_range: str = ""
    branch: str = ""
    commit: str = ""
    agent: str = ""
    duration_seconds: float = 0.0
    #: Parsed security findings the agent reported.
    findings: list[dict[str, Any]] = field(default_factory=list)
    #: Parsed non-security bugs the agent reported.
    bugs: list[dict[str, Any]] = field(default_factory=list)
    #: How many findings were filed as SecurityReport rows.
    finding_count: int = 0
    #: How many non-security bugs were filed as IntroducedBug rows.
    bug_count: int = 0
    #: How many of those were queued for exploit verification.
    verified_count: int = 0
    #: How many were linked to an existing report in the project (cross-source
    #: dedup: a paste, email or zip report of the same hole the scan just found).
    linked_count: int = 0
    #: How many agent findings were not filed (no title, or a re-scan of a
    #: finding the scan already filed — the self-dedup skip).
    skipped_count: int = 0
    raw_output: str = ""

    def summary(self) -> str:
        """One line for the worker log and ``IntroducedScanRun.log``."""
        if self.error:
            return f"introduced scan: did not run — {self.error}"
        bugs = f", {self.bug_count} bug(s) filed" if self.bug_count else ""
        links = f", {self.linked_count} linked" if self.linked_count else ""
        skipped = f", {self.skipped_count} skipped" if self.skipped_count else ""
        return (
            f"introduced scan ({self.cadence}, {self.commit_range}): "
            f"{self.finding_count} finding(s) filed, {self.verified_count} queued for verify"
            f"{links}{skipped}{bugs}"
        )


def is_due(
    project: Project,
    config: IntroducedScanConfig,
    *,
    now: Any | None = None,
) -> bool:
    """Whether *project*'s scan is due to run under its cadence.

    Reads the most recent :class:`IntroducedScanRun` for the project and compares
    ``started_at`` to the cadence interval. A project with no prior run is due
    immediately (the operator just opted in). A run that started but never
    finished counts as the last run too — it's not due again until the interval
    passes, so a crash doesn't trigger a retry storm every cycle.
    """
    if not config.enabled:
        return False
    interval = CADENCE_INTERVAL.get(config.cadence)
    if interval is None:
        return False
    now = now or timezone.now()
    if config.cadence == "month-end" and not _month_end_due(project, now):
        return False
    last = IntroducedScanRun.objects.filter(project=project).order_by("-started_at").first()
    if last is None:
        return True
    return now - last.started_at >= interval


def scan_project_for_introduced_cves(
    project: Project,
    operator_config: OperatorConfig,
    *,
    cadence: str | None = None,
) -> IntroducedScanResult:
    """Scan *project*'s recent changes for newly introduced CVEs.

    Never raises: every early return carries a reason. Creates an
    :class:`IntroducedScanRun` row at the start (so a crash leaves a visible
    "started, never finished" row) and fills it in at the end.
    """
    from franktheunicorn.config.loader import get_project_config
    from franktheunicorn.review.tool_executor import make_executor

    result = IntroducedScanResult()
    triage = operator_config.security_triage
    verifier = triage.verifier

    project_config = get_project_config(project.full_name)
    if project_config is None:
        result.error = f"no project config for {project.full_name}"
        logger.info("Introduced scan skipped: %s", result.error)
        return result
    config = project_config.introduced_scan
    if not config.enabled:
        result.error = "introduced_scan.enabled is false for this project"
        logger.info("Introduced scan skipped for %s: %s", project.full_name, result.error)
        return result

    cadence = (cadence or config.cadence).strip().lower()
    if cadence not in CADENCE_INTERVAL:
        result.error = f"cadence {cadence!r} is not daily/weekly/monthly"
        logger.warning("Introduced scan for %s: %s", project.full_name, result.error)
        return result
    result.cadence = cadence

    reviewer = resolve_verifier_reviewer(operator_config, verifier)
    if reviewer is None:
        result.error = f"no agent_cli_reviewers entry named {verifier.reviewer!r}"
        logger.warning("Introduced scan for %s: %s", project.full_name, result.error)
        return result

    # Record the run up front: a crash here leaves a "started, never finished"
    # row rather than looking like the scan never happened.
    run_row = IntroducedScanRun.objects.create(
        project=project, cadence=cadence, started_at=timezone.now()
    )

    def _finish(res: IntroducedScanResult) -> IntroducedScanResult:
        run_row.finished_at = timezone.now()
        run_row.cadence = res.cadence
        run_row.commit_range = res.commit_range
        run_row.finding_count = res.finding_count
        run_row.bug_count = res.bug_count
        run_row.verified_count = res.verified_count
        run_row.log = res.summary()
        run_row.error = res.error
        run_row.save()
        return res

    executor = make_executor(reviewer.remote)
    cwd = executor.prepare_repo(
        project.owner,
        project.repo,
        local_path=_local_checkout(project.owner, project.repo),
        workspace_subdir=config.workspace_subdir,
    )
    if not cwd:
        result.error = (
            "no checkout could be prepared — for remote.mode ssh check ssh_command "
            "and remote_workspace_dir; for local mode check FRANK_REPOS_DIR"
        )
        logger.warning("Introduced scan for %s: %s", project.full_name, result.error)
        return _finish(result)

    stale = refresh_from_upstream(executor, cwd)
    if stale:
        logger.warning(
            "Could not refresh %s before scanning %s (%s). Going ahead against "
            "whatever the checkout already had.",
            cwd,
            project.full_name,
            stale,
        )

    branch = _default_branch(executor, cwd)
    if not branch:
        result.error = "no default branch could be resolved in the checkout"
        logger.warning("Introduced scan for %s: %s", project.full_name, result.error)
        return _finish(result)
    result.branch = branch

    commit = _checkout(executor, cwd, branch, fresh=False)
    if not commit:
        result.error = f"could not check out origin/{branch}"
        logger.warning("Introduced scan for %s: %s", project.full_name, result.error)
        return _finish(result)
    result.commit = commit

    # The commit range: last N days on daily, diff from the last final release
    # tag on weekly/monthly.
    if cadence == "daily":
        range_label, diff = _daily_diff(executor, cwd, config.lookback_days)
    else:
        range_label, diff = _since_release_diff(executor, cwd, project, project_config)
    result.commit_range = range_label
    if diff is None:
        # Nothing to review (no commits in the window, or no release to diff
        # from). That's a clean run, not an error.
        logger.info(
            "Introduced scan for %s: %s — nothing to review",
            project.full_name,
            range_label,
        )
        return _finish(result)
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + "\n…[truncated]"

    accepted, rejected = _prior_context(project)
    security_model = project_config.security_model or "(none set)"
    review_guidance = (
        resolve_review_guidance(project)
        or "(none yet — build it with manage.py build_review_guidance)"
    )
    magpie_skills = skills_block_for_project(project, inject_into="security")
    from franktheunicorn.security.cve_class_catalog import resolve_cve_class_catalog
    from franktheunicorn.security.introduced_scan_prompt import (
        BUG_SECTION,
        BUG_SECTION_OFF,
        CVE_CLASS_SECTION,
        CVE_CLASS_SECTION_OFF,
    )

    bug_section = BUG_SECTION if config.detect_bugs else BUG_SECTION_OFF
    cve_catalog = resolve_cve_class_catalog(project_config)
    cve_class_section = (
        CVE_CLASS_SECTION.format(cve_class_catalog=cve_catalog)
        if cve_catalog
        else CVE_CLASS_SECTION_OFF
    )
    prompt = _build_prompt(
        project=project.full_name,
        branch=branch,
        commit_range=range_label,
        accepted_context=accepted,
        rejected_context=rejected,
        security_model=security_model,
        review_guidance=review_guidance,
        diff=diff,
        magpie_skills=magpie_skills,
        addendum=config.prompt_addendum,
        bug_section=bug_section,
        cve_class_catalog=cve_class_section,
    )

    # The model gauntlet: try each model in order until one parses, or the
    # timeline runs out. Empty ``models`` means one attempt with ``model`` (or
    # the verifier's / reviewer's), the original behavior.
    models_to_try = config.models or [config.model or verifier.model or reviewer.model or "default"]
    timeline_started = time.monotonic()
    last_attempt: _ModelAttempt | None = None
    for model_name in models_to_try:
        # Record the model up front so a failed/timeline run still shows which
        # model was attempted (the original code set agent before the run).
        result.agent = f"{reviewer.name}/{model_name}"
        if (
            config.max_timeline_seconds > 0
            and time.monotonic() - timeline_started >= config.max_timeline_seconds
        ):
            result.error = (
                f"max timeline ({config.max_timeline_seconds}s) exceeded before trying {model_name}"
            )
            break
        attempt = _run_model_attempt(
            executor, cwd, reviewer, config, prompt, model_name, project.full_name
        )
        result.duration_seconds += attempt.duration_seconds
        last_attempt = attempt
        if not attempt.error and attempt.parsed:
            break  # success — use this attempt's findings
        if attempt.error:
            logger.warning(
                "Introduced scan for %s: model %s failed (%s); trying next",
                project.full_name,
                model_name,
                attempt.error,
            )
        else:
            # No JSON but the agent ran — soft failure. Try the next model;
            # if this is the last one the post-loop block accepts it as clean.
            logger.info(
                "Introduced scan for %s: model %s produced no JSON; trying next",
                project.full_name,
                model_name,
            )

    if last_attempt is None:
        # Timeline exceeded before the first model ran.
        return _finish(result)
    if result.error:
        # Timeline broke the loop — result.error already carries the timeline
        # reason; don't overwrite it with the last attempted model's error.
        return _finish(result)
    if last_attempt.error:
        # Every model failed with a real error — the last one's error wins.
        result.error = last_attempt.error
        result.raw_output = last_attempt.raw_output
        return _finish(result)

    # A parsed attempt, or the last model's no-JSON (accepted as a clean run).
    result.findings = last_attempt.findings
    result.bugs = last_attempt.bugs
    result.raw_output = last_attempt.raw_output

    from franktheunicorn.security.queue import queue_exploit_verification

    dup_config = operator_config.security_triage.duplicates
    filed = 0
    verified = 0
    linked = 0
    skipped = 0
    for finding in result.findings[: config.max_findings]:
        report = _file_finding(finding, project, run_row, dup_config)
        if report is None:
            skipped += 1
            continue
        filed += 1
        if report.duplicate_of_id is not None:
            linked += 1
        if config.auto_verify and queue_exploit_verification(report):
            verified += 1
            logger.info(
                "Queued exploit verification for introduced-scan report #%s on %s",
                report.pk,
                project.full_name,
            )
    result.finding_count = filed
    result.verified_count = verified
    result.linked_count = linked
    result.skipped_count = skipped

    bugs_filed = 0
    if config.detect_bugs:
        for bug in result.bugs[: config.max_bugs]:
            if _file_bug(bug, project, run_row, result.commit) is not None:
                bugs_filed += 1
    result.bug_count = bugs_filed
    logger.info("Introduced scan for %s: %s", project.full_name, result.summary())
    return _finish(result)


def _daily_diff(executor: ToolExecutor, cwd: str, lookback_days: int) -> tuple[str, str | None]:
    """The diff of the last *lookback_days* of commits on the current branch.

    Returns ``(range_label, diff)`` where diff is None when there are no commits
    in the window. Uses ``git log --since`` to find the oldest commit in the
    window, then diffs from its parent to HEAD — the cumulative change.
    """
    since = f"{lookback_days} days ago"
    log = executor.run(
        ["git", "log", f"--since={since}", "--reverse", "--format=%H"],
        cwd=cwd,
        timeout=60,
    )
    if log is None or not log.ok or not log.stdout.strip():
        return f"last {lookback_days} day(s)", None
    first = log.stdout.splitlines()[0].strip()
    diff = executor.run(
        ["git", "diff", f"{first}^..HEAD"],
        cwd=cwd,
        timeout=120,
    )
    if diff is None or not diff.ok:
        return f"last {lookback_days} day(s)", None
    return f"last {lookback_days} day(s) ({first[:12]}..HEAD)", diff.stdout


def _last_final_release(executor: ToolExecutor, cwd: str, project: Project) -> str:
    """The most recent final release tag in the checkout, or "" if none.

    ``--sort=-v:refname`` is descending by version, so the first final tag is
    the newest. RCs and unrecognised schemes are filtered by the project's own
    release-tag rule (``release_tag_prefix`` / ``release_tag_pattern``).
    """
    from franktheunicorn.security.release_tags import FINAL, release_tag_rule_for

    rule = release_tag_rule_for(project.full_name)
    result = executor.run(
        ["git", "tag", "--sort=-v:refname"],
        cwd=cwd,
        timeout=60,
    )
    if result is None or not result.ok:
        return ""
    for tag in result.stdout.splitlines():
        tag = tag.strip()
        if tag and rule.classify(tag) == FINAL:
            return tag
    return ""


def _since_release_diff(
    executor: ToolExecutor, cwd: str, project: Project, project_config: ProjectConfig
) -> tuple[str, str | None]:
    """The diff from the last final release tag to HEAD.

    Returns ``(range_label, diff)`` where diff is None when there is no release
    tag to diff from (a project that has never cut a final release). The label
    names the tag so the run row records what was compared.
    """
    tag = _last_final_release(executor, cwd, project)
    if not tag:
        return "since last release (none found)", None
    diff = executor.run(
        ["git", "diff", f"{tag}..HEAD"],
        cwd=cwd,
        timeout=120,
    )
    if diff is None or not diff.ok:
        return f"{tag}..HEAD", None
    return f"{tag}..HEAD", diff.stdout


def _prior_context(project: Project) -> tuple[str, str]:
    """The project's previously accepted and rejected CVE reports as text.

    Accepted = ruled valid (with a CVE id when one is assigned). Rejected =
    invalid / expected-behavior / duplicate. Each is a one-line summary plus a
    short excerpt, capped so a big backlog doesn't drown the prompt.
    """
    reports = SecurityReport.objects.filter(project=project).exclude(status="new")
    accepted: list[str] = []
    rejected: list[str] = []
    for r in reports.order_by("-created_at"):
        cve = f" [{r.matched_cve_id}]" if r.matched_cve_id else ""
        line = f"- {r.title}{cve} ({r.status})"
        excerpt = (r.raw_text or "").strip().replace("\n", " ")[:_MAX_CONTEXT_CHARS]
        block = f"{line}\n  {excerpt}" if excerpt else line
        if r.status == "valid" and len(accepted) < 30:
            accepted.append(block)
        elif r.status in SecurityReport.NO_FIX_OWED_STATUSES and len(rejected) < 30:
            rejected.append(block)
    return (
        "\n".join(accepted) or "(none)",
        "\n".join(rejected) or "(none)",
    )


def _build_prompt(
    *,
    project: str,
    branch: str,
    commit_range: str,
    accepted_context: str,
    rejected_context: str,
    security_model: str,
    review_guidance: str,
    diff: str,
    magpie_skills: str,
    addendum: str,
    bug_section: str = "",
    cve_class_catalog: str = "",
) -> str:
    addendum_block = ""
    if addendum:
        addendum_block = ADDENDUM_TEMPLATE.format(addendum=addendum)
    return _PROMPT_TEMPLATE.format(
        project=project,
        branch=branch,
        commit_range=commit_range,
        accepted_context=accepted_context,
        rejected_context=rejected_context,
        security_model=security_model,
        review_guidance=review_guidance,
        diff=diff,
        magpie_skills=magpie_skills,
        addendum=addendum_block,
        bug_section=bug_section,
        cve_class_catalog=cve_class_catalog,
    )


@dataclass
class _ModelAttempt:
    """The outcome of one model attempt in the multi-model gauntlet.

    ``error`` non-empty means the agent did not run (timeout, refusal, non-zero
    exit) — the gauntlet should try the next model. ``parsed`` is False when the
    agent ran but produced no JSON — a soft failure that tries the next model,
    unless this is the last one (then the caller accepts it as a clean run, the
    original single-model behavior).
    """

    error: str = ""
    parsed: bool = False
    findings: list[dict[str, Any]] = field(default_factory=list)
    bugs: list[dict[str, Any]] = field(default_factory=list)
    raw_output: str = ""
    duration_seconds: float = 0.0
    model: str = ""


def _run_model_attempt(
    executor: ToolExecutor,
    cwd: str,
    reviewer: AgentCLIReviewerConfig,
    config: IntroducedScanConfig,
    prompt: str,
    model: str,
    project_name: str,
) -> _ModelAttempt:
    """Run the agent with one model. Returns the parsed findings or an error.

    Never raises — every failure path carries a reason. The caller loops over
    models and stops at the first one that parses (or accepts the last one's
    no-JSON as a clean run).
    """
    from franktheunicorn.review.tool_executor import (
        agent_stderr,
        auth_failure_advice,
        guard_agent_argv,
        guard_notes,
        guard_refusal,
        looks_like_auth_failure,
        looks_like_workspace_trust_refusal,
        workspace_trust_advice,
    )

    attempt = _ModelAttempt(model=model)
    try:
        started = time.monotonic()
        invocation = _invocation(reviewer, config, model, prompt)
        # A prompt over the argv limit goes via stdin: build_invocation puts
        # the prompt last, so strip it and hand the text to executor.run.
        if len(prompt) > _MAX_ARG_PROMPT_CHARS:
            argv = [*reviewer.cli_argv, *invocation[:-1]]
            stdin = prompt
            logger.info(
                "Introduced scan for %s: prompt is %d chars; delivering via stdin",
                project_name,
                len(prompt),
            )
        else:
            argv = [*reviewer.cli_argv, *invocation]
            stdin = None
        run_result = executor.run(
            guard_agent_argv(argv, stdin=stdin is not None),
            cwd=cwd,
            timeout=config.timeout_seconds,
            stdin=stdin,
        )
        attempt.duration_seconds = time.monotonic() - started
        for note in guard_notes(run_result.stderr if run_result is not None else ""):
            logger.warning("Introduced scan for %s: %s", project_name, note)
        if run_result is not None and (
            refused := guard_refusal(run_result.returncode, run_result.stderr)
        ):
            attempt.error = f"the {reviewer.name} CLI did not run: {refused}"
            return attempt
        if run_result is None:
            attempt.error = (
                f"the {reviewer.name} CLI produced no result within {config.timeout_seconds}s"
            )
            return attempt
        if not run_result.ok:
            stderr = agent_stderr(run_result.stderr)
            detail = (stderr or run_result.stdout).strip()[:500]
            summary = f"the {reviewer.name} CLI exited {run_result.returncode}: {detail}"
            if looks_like_auth_failure(stderr):
                summary = (
                    f"{summary}\n\n"
                    f"{auth_failure_advice(reviewer.name, reviewer.cli_argv[0], remote=reviewer.remote.mode == 'ssh')}"
                )
            if looks_like_workspace_trust_refusal(stderr, run_result.stdout):
                summary = f"{summary}\n\n{workspace_trust_advice(reviewer.name)}"
            attempt.error = summary
            attempt.raw_output = run_result.stdout[:20_000]
            return attempt

        parsed = _parse_findings(run_result.stdout)
        attempt.raw_output = run_result.stdout[:20_000]
        if parsed is None:
            logger.warning(
                "No JSON findings in the %s output for %s; keeping the raw text.",
                reviewer.name,
                project_name,
            )
            return attempt  # parsed=False, error="" — soft failure
        attempt.parsed = True
        attempt.findings = parsed.get("findings", []) if isinstance(parsed, dict) else []
        attempt.bugs = parsed.get("bugs", []) if isinstance(parsed, dict) else []
        return attempt
    except Exception as exc:
        logger.exception("Introduced scan for %s: model %s crashed", project_name, model)
        attempt.error = f"model {model} crashed: {exc}"
        return attempt


def _invocation(
    reviewer: AgentCLIReviewerConfig,
    config: IntroducedScanConfig,
    model: str,
    prompt: str,
) -> list[str]:
    """The reviewer's argv shape, with the given model and the scan's flags.

    The model is passed in (not read from ``config``) so the multi-model
    gauntlet can call this once per model. ``extra_args`` still come from the
    scan config — they are shared across attempts.
    """
    borrowed = reviewer.model_copy(
        update={
            "model": model,
            "extra_args": [*reviewer.extra_args, *config.extra_args],
        }
    )
    return borrowed.build_invocation(prompt)


def _parse_findings(output: str) -> dict[str, Any] | None:
    """The JSON object the agent was asked for, or None when no JSON is found.

    Returns the full parsed dict (with ``findings`` and ``bugs`` keys) so the
    caller can pull both lists. Lenient like the siblings' parsers: a model
    told to emit only JSON will still wrap it in prose. None rather than an
    empty dict so an unparseable answer is distinguishable from a clean run.
    """
    import json

    from franktheunicorn.review.backends.base import json_object_candidates

    for blob in json_object_candidates(output):
        try:
            candidate = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(candidate, dict) and "findings" in candidate:
            # Filter non-dict entries from both lists, the way the old parser did.
            findings = candidate.get("findings", [])
            if isinstance(findings, list):
                candidate["findings"] = [f for f in findings if isinstance(f, dict)]
            else:
                candidate["findings"] = []
            bugs = candidate.get("bugs", [])
            if isinstance(bugs, list):
                candidate["bugs"] = [b for b in bugs if isinstance(b, dict)]
            else:
                candidate["bugs"] = []
            return candidate
    return None


def _file_finding(
    finding: dict[str, Any],
    project: Project,
    run_row: IntroducedScanRun,
    dup_config: SecurityDuplicateConfig | None = None,
) -> SecurityReport | None:
    """File one finding as a SecurityReport with source="introduced-scan".

    Returns the report, or None when the finding had no title or is a re-scan of
    a finding the scan already filed (same title + source + project, and the prior
    one is still open). After filing, links the report to an existing one in the
    project when the duplicate heuristic scores them above threshold — so a paste,
    email or zip report of the same hole the scan just found is surfaced as a
    candidate link rather than filed as a second row the operator triages twice.
    The link is a pointer to any earlier report (triaged or not — surfacing "same
    hole, already ruled on" is as useful as "same hole, untriaged"), never a
    verdict (``status="duplicate"`` stays the operator's); see ``security.duplicates``.
    """
    title = str(finding.get("title", "")).strip()
    if not title:
        return None
    # Self-dedup: a prior introduced-scan report with the same title that is still
    # open means the last run already filed this one — skip, don't re-file. A
    # prior report the operator dismissed (invalid / expected-behavior / closed)
    # does NOT block: the scan window moved, and the same title in a new range
    # may be a genuinely new introduction worth re-surfacing.
    if (
        SecurityReport.objects.filter(project=project, source="introduced-scan", title=title[:500])
        .exclude(status__in=SecurityReport.NO_FIX_OWED_STATUSES)
        .exists()
    ):
        logger.info(
            "Introduced scan skipping a re-scan of an existing open scan finding: %s",
            title,
        )
        return None
    severity = str(finding.get("severity", "")).strip().lower() or "unknown"
    component = str(finding.get("component", "")).strip()
    paths = finding.get("suspected_paths", [])
    paths_text = "\n".join(f"  - {p}" for p in paths if isinstance(p, str)) if paths else ""
    description = str(finding.get("description", "")).strip()
    reasoning = str(finding.get("reasoning", "")).strip()
    body = "\n\n".join(
        part
        for part in (
            f"Component: {component}" if component else "",
            f"Severity: {severity}",
            f"Suspected paths:\n{paths_text}" if paths_text else "",
            description,
            f"Reasoning (why this change introduced it):\n{reasoning}" if reasoning else "",
            f"Filed by the introduced-CVE scan (cadence {run_row.cadence}, "
            f"range {run_row.commit_range}).",
        )
        if part
    )
    report = SecurityReport.objects.create(
        project=project,
        source="introduced-scan",
        status="new",
        title=title[:500],
        claimed_severity=severity,
        parsed_component=component,
        raw_text=body,
    )
    # Cross-source dedup: link to an existing report in the project when the
    # heuristic scores them above threshold. Heuristic only (no LLM call) — the
    # scan already ran an agent to find the CVE; this is a cheap local match
    # against rows already in the database. Respects the operator's duplicates
    # config (off → no link). Never sets status="duplicate" — a pointer, not a
    # verdict.
    if dup_config is not None and dup_config.enabled:
        from franktheunicorn.security.duplicates import detect_for_report

        detection = detect_for_report(report, dup_config)
        if detection.match is not None and report.duplicate_of_id is not None:
            logger.info(
                "Introduced-scan report #%s looks like a duplicate of #%s "
                "(score %.2f: %s) — linked, not merged.",
                report.pk,
                detection.match.report_id,
                detection.match.score,
                detection.match.reason,
            )
    return report


def _file_bug(
    bug: dict[str, Any],
    project: Project,
    run_row: IntroducedScanRun,
    commit: str,
) -> IntroducedBug | None:
    """File one non-security bug as an IntroducedBug with a proposed patch.

    Returns the bug, or None when the bug had no title. Dedup is left to the
    operator — a re-scan over the same range will file again.
    """
    from franktheunicorn.core.models import IntroducedBug

    title = str(bug.get("title", "")).strip()
    if not title:
        return None
    category = str(bug.get("category", "")).strip().lower() or "other"
    severity = str(bug.get("severity", "")).strip().lower() or "minor"
    file_path = str(bug.get("file_path", "")).strip()[:500]
    line_start = bug.get("line_start")
    line_end = bug.get("line_end")
    description = str(bug.get("description", "")).strip()
    proposed_patch = str(bug.get("proposed_patch", "")).strip()
    row = IntroducedBug.objects.create(
        project=project,
        scan_run=run_row,
        commit=commit[:40],
        file_path=file_path,
        line_start=int(line_start) if isinstance(line_start, (int, float)) else None,
        line_end=int(line_end) if isinstance(line_end, (int, float)) else None,
        category=category[:30],
        severity=severity[:20],
        title=title[:500],
        description=description,
        proposed_patch=proposed_patch,
        status="new",
    )
    return row
