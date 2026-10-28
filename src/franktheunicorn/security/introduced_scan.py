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
from datetime import timedelta
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
    )
    from franktheunicorn.core.models import IntroducedBug, Project
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

#: Cap the diff in the prompt. A day of Spark is large; a week is larger. The
#: agent gets the most recent part and a note that it was truncated.
_MAX_DIFF_CHARS = 200_000

#: How much of each prior CVE report's text to include as context.
_MAX_CONTEXT_CHARS = 4_000

#: Cadence → minimum time between runs. The scheduler reads this.
CADENCE_INTERVAL: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}


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
    raw_output: str = ""

    def summary(self) -> str:
        """One line for the worker log and ``IntroducedScanRun.log``."""
        if self.error:
            return f"introduced scan: did not run — {self.error}"
        bugs = f", {self.bug_count} bug(s) filed" if self.bug_count else ""
        return (
            f"introduced scan ({self.cadence}, {self.commit_range}): "
            f"{self.finding_count} finding(s) filed, {self.verified_count} queued for verify{bugs}"
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
    from franktheunicorn.security.introduced_scan_prompt import BUG_SECTION, BUG_SECTION_OFF

    bug_section = BUG_SECTION if config.detect_bugs else BUG_SECTION_OFF
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
    )

    started = time.monotonic()
    result.agent = (
        f"{reviewer.name}/{config.model or verifier.model or reviewer.model or 'default'}"
    )
    argv = [*reviewer.cli_argv, *_invocation(reviewer, config, verifier.model, prompt)]
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

    run_result = executor.run(guard_agent_argv(argv), cwd=cwd, timeout=config.timeout_seconds)
    result.duration_seconds = time.monotonic() - started
    for note in guard_notes(run_result.stderr if run_result is not None else ""):
        logger.warning("Introduced scan for %s: %s", project.full_name, note)
    if run_result is not None and (
        refused := guard_refusal(run_result.returncode, run_result.stderr)
    ):
        result.error = f"the {reviewer.name} CLI did not run: {refused}"
        return _finish(result)
    if run_result is None:
        result.error = (
            f"the {reviewer.name} CLI produced no result within {config.timeout_seconds}s"
        )
        return _finish(result)
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
        result.error = summary
        result.raw_output = run_result.stdout[:20_000]
        return _finish(result)

    parsed = _parse_findings(run_result.stdout)
    if parsed is None:
        logger.warning(
            "No JSON findings in the %s output for %s; keeping the raw text.",
            reviewer.name,
            project.full_name,
        )
        result.findings = []
        result.bugs = []
        result.raw_output = run_result.stdout[:20_000]
    else:
        result.findings = parsed.get("findings", []) if isinstance(parsed, dict) else []
        result.bugs = parsed.get("bugs", []) if isinstance(parsed, dict) else []

    from franktheunicorn.security.queue import queue_exploit_verification

    filed = 0
    verified = 0
    for finding in result.findings[: config.max_findings]:
        report = _file_finding(finding, project, run_row)
        if report is None:
            continue
        filed += 1
        if config.auto_verify and queue_exploit_verification(report):
            verified += 1
            logger.info(
                "Queued exploit verification for introduced-scan report #%s on %s",
                report.pk,
                project.full_name,
            )
    result.finding_count = filed
    result.verified_count = verified

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
    )


def _invocation(
    reviewer: AgentCLIReviewerConfig,
    config: IntroducedScanConfig,
    verifier_model: str,
    prompt: str,
) -> list[str]:
    """The reviewer's argv shape, with the scan config's model and flags.

    Mirrors the siblings: the model falls back to the verifier's, so
    ``introduced_scan.model`` empty + ``verifier.model`` set uses the verifier's.
    """
    borrowed = reviewer.model_copy(
        update={
            "model": config.model or verifier_model or reviewer.model,
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
) -> SecurityReport | None:
    """File one finding as a SecurityReport with source="introduced-scan".

    Returns the report, or None when the finding had no title (a finding with no
    title is not a finding the operator can triage). Dedup is left to the
    operator — a re-scan over the same range will file again, and the operator
    dismisses the dup the way they dismiss any other.
    """
    title = str(finding.get("title", "")).strip()
    if not title:
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
