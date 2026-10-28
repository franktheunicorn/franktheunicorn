"""Variant-analysis scan: find siblings of a known CVE elsewhere in the codebase.

One agent per confirmed past CVE (a valid SecurityReport with a dated
``introduced_commit``) hunts for the same bug pattern elsewhere — the Trail of
Bits Rust pipeline's core technique. The prompt gives the agent a one-sentence
risk class (the report's title) alongside the introducing diff: the diff shows
the code shape, the risk class keeps the agent from over-matching the exact
bug and missing neighbours. Each candidate lands as a SecurityReport with
source="variant-scan" and is queued for verification (the second-pass gauntlet).
Read-only, borrows the verifier's checkout, never raises.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from franktheunicorn.core.models import SecurityReport, VariantScanRun
from franktheunicorn.magpie.skills import skills_block_for_project
from franktheunicorn.security.review_guidance import resolve_review_guidance
from franktheunicorn.security.triage import resolve_security_model
from franktheunicorn.security.variant_scan_prompt import ADDENDUM_TEMPLATE
from franktheunicorn.security.variant_scan_prompt import (
    TEMPLATE as _PROMPT_TEMPLATE,
)
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
        OperatorConfig,
        VariantScanConfig,
    )
    from franktheunicorn.core.models import Project
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

#: Cap the introduced commit's diff in the prompt.
_MAX_DIFF_CHARS = 50_000

CADENCE_INTERVAL: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}


@dataclass
class VariantScanResult:
    """The outcome of one scan run for one project."""

    error: str = ""
    cadence: str = "weekly"
    branch: str = ""
    agent: str = ""
    duration_seconds: float = 0.0
    cves_examined: int = 0
    finding_count: int = 0
    verified_count: int = 0
    refused_count: int = 0
    failed_count: int = 0
    unparseable_count: int = 0
    raw_output: str = ""

    def summary(self) -> str:
        if self.error:
            return f"variant scan: did not run — {self.error}"
        parts = [
            f"variant scan ({self.cadence}): examined {self.cves_examined} CVE(s)",
            f"filed {self.finding_count} variant(s)",
            f"{self.verified_count} queued for verify",
        ]
        if self.refused_count:
            parts.append(f"{self.refused_count} refused")
        if self.failed_count:
            parts.append(f"{self.failed_count} failed")
        if self.unparseable_count:
            parts.append(f"{self.unparseable_count} unparseable")
        return ", ".join(parts)


def is_due(
    project: Project,
    config: VariantScanConfig,
    *,
    now: Any | None = None,
) -> bool:
    """Whether *project*'s variant scan is due under its cadence.

    Same shape as the introduced scan's is_due: reads the most recent
    VariantScanRun and compares started_at to the cadence interval.
    """
    if not config.enabled:
        return False
    interval = CADENCE_INTERVAL.get(config.cadence)
    if interval is None:
        return False
    now = now or timezone.now()
    last = VariantScanRun.objects.filter(project=project).order_by("-started_at").first()
    if last is None:
        return True
    return now - last.started_at >= interval


def _variant_candidates(
    project: Project,
    config: VariantScanConfig,
) -> list[SecurityReport]:
    """The confirmed past CVEs to variant-hunt from, rotated across runs.

    A valid report with a dated introduced_commit, excluding reports filed by
    this scan (a variant ruled valid shouldn't seed further variants — that
    compounds). Rotated by run count so a backlog bigger than max_cves_per_run
    is eventually covered: run N examines the slice offset by
    (N * max) mod total, wrapping. Capped at max_cves_per_run.
    """
    qs = (
        SecurityReport.objects.filter(project=project, status="valid")
        .exclude(introduced_commit="")
        .exclude(source="variant-scan")
        .order_by("introduced_at", "created_at")
    )
    eligible = list(qs)
    if not eligible:
        return []
    prior_runs = VariantScanRun.objects.filter(project=project).count()
    offset = (prior_runs * config.max_cves_per_run) % len(eligible)
    rotated = eligible[offset:] + eligible[:offset]
    return rotated[: config.max_cves_per_run]


def _risk_class(report: SecurityReport) -> str:
    """A one-sentence description of the bug pattern, from the report.

    The title is the risk class; the triage summary adds the "how" when one
    exists. Paired with the introducing diff in the prompt — the diff shows
    the code shape, this keeps the agent from over-matching the exact bug.
    """
    summary = (report.triage_summary or "").strip()
    if summary:
        return f"{report.title}: {summary[:300]}"
    return report.title or "(untitled)"


def _introduced_diff(executor: ToolExecutor, cwd: str, commit: str) -> str:
    """The diff that introduced the CVE, via git show. Capped."""
    show = executor.run(
        ["git", "show", "--no-color", commit[:40]],
        cwd=cwd,
        timeout=120,
    )
    if show is None or not show.ok:
        return f"(could not show commit {commit[:12]})"
    diff = show.stdout
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + "\n…[truncated]"
    return diff


def scan_project_for_variants(
    project: Project,
    operator_config: OperatorConfig,
    *,
    cadence: str | None = None,
) -> VariantScanResult:
    """Scan *project* for variants of its confirmed past CVEs.

    Never raises: every early return carries a reason. Creates a VariantScanRun
    row at the start (so a crash leaves a visible "started, never finished" row).
    """
    from franktheunicorn.config.loader import get_project_config
    from franktheunicorn.review.tool_executor import make_executor

    result = VariantScanResult()
    triage = operator_config.security_triage
    verifier = triage.verifier

    project_config = get_project_config(project.full_name)
    if project_config is None:
        result.error = f"no project config for {project.full_name}"
        logger.info("Variant scan skipped: %s", result.error)
        return result
    config = project_config.variant_scan
    if not config.enabled:
        result.error = "variant_scan.enabled is false for this project"
        logger.info("Variant scan skipped for %s: %s", project.full_name, result.error)
        return result

    cadence = (cadence or config.cadence).strip().lower()
    if cadence not in CADENCE_INTERVAL:
        result.error = f"cadence {cadence!r} is not daily/weekly/monthly"
        logger.warning("Variant scan for %s: %s", project.full_name, result.error)
        return result
    result.cadence = cadence

    # Create the run row before any decline that depends on data or config, so
    # the cadence clock starts even when there is nothing to scan yet —
    # otherwise the scheduler re-queues every cycle.
    run_row = VariantScanRun.objects.create(
        project=project, cadence=cadence, started_at=timezone.now()
    )

    def _finish(res: VariantScanResult) -> VariantScanResult:
        run_row.finished_at = timezone.now()
        run_row.cadence = res.cadence
        run_row.cves_examined = res.cves_examined
        run_row.finding_count = res.finding_count
        run_row.verified_count = res.verified_count
        run_row.log = res.summary()
        run_row.error = res.error
        run_row.save()
        return res

    candidates = _variant_candidates(project, config)
    if not candidates:
        result.error = "no valid report with a dated introduced_commit to variant-hunt from"
        logger.info("Variant scan for %s: %s", project.full_name, result.error)
        return _finish(result)

    reviewer = resolve_verifier_reviewer(operator_config, verifier)
    if reviewer is None:
        result.error = f"no agent_cli_reviewers entry named {verifier.reviewer!r}"
        logger.warning("Variant scan for %s: %s", project.full_name, result.error)
        return _finish(result)

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
        logger.warning("Variant scan for %s: %s", project.full_name, result.error)
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
        logger.warning("Variant scan for %s: %s", project.full_name, result.error)
        return _finish(result)
    result.branch = branch

    commit = _checkout(executor, cwd, branch, fresh=False)
    if not commit:
        result.error = f"could not check out origin/{branch}"
        logger.warning("Variant scan for %s: %s", project.full_name, result.error)
        return _finish(result)

    security_model = resolve_security_model(project_config) or "(none set)"
    review_guidance = (
        resolve_review_guidance(project)
        or "(none yet — build it with manage.py build_review_guidance)"
    )
    magpie_skills = skills_block_for_project(project, inject_into="security")

    from franktheunicorn.review.tool_executor import (
        agent_stderr,
        guard_agent_argv,
        guard_notes,
        guard_refusal,
    )
    from franktheunicorn.security.queue import queue_verification

    started = time.monotonic()
    result.agent = (
        f"{reviewer.name}/{config.model or verifier.model or reviewer.model or 'default'}"
    )
    total_filed = 0
    total_verified = 0
    total_refused = 0
    total_failed = 0
    total_unparseable = 0
    raw_outputs: list[str] = []

    for cve_report in candidates:
        cve_diff = _introduced_diff(executor, cwd, cve_report.introduced_commit)
        prompt = _build_prompt(
            project=project.full_name,
            branch=branch,
            cve_title=cve_report.title or "(untitled)",
            risk_class=_risk_class(cve_report),
            cve_component=cve_report.parsed_component or "(not stated)",
            introduced_commit=cve_report.introduced_commit[:12],
            introduced_diff=cve_diff,
            security_model=security_model,
            review_guidance=review_guidance,
            magpie_skills=magpie_skills,
            addendum=config.prompt_addendum,
        )
        argv = [*reviewer.cli_argv, *_invocation(reviewer, config, verifier.model, prompt)]
        run_result = executor.run(guard_agent_argv(argv), cwd=cwd, timeout=config.timeout_seconds)
        for note in guard_notes(run_result.stderr if run_result is not None else ""):
            logger.warning(
                "Variant scan for %s on CVE #%s: %s", project.full_name, cve_report.pk, note
            )
        if run_result is not None and (
            refused := guard_refusal(run_result.returncode, run_result.stderr)
        ):
            total_refused += 1
            logger.warning(
                "Variant scan for %s on CVE #%s: refused: %s",
                project.full_name,
                cve_report.pk,
                refused,
            )
            raw_outputs.append(f"CVE #{cve_report.pk}: refused")
            continue
        if run_result is None or not run_result.ok:
            total_failed += 1
            detail = ""
            if run_result is not None:
                detail = agent_stderr(run_result.stderr).strip()[:500]
            logger.warning(
                "Variant scan for %s on CVE #%s: no result or non-ok exit",
                project.full_name,
                cve_report.pk,
            )
            raw_outputs.append(f"CVE #{cve_report.pk}: failed — {detail}")
            continue

        parsed = _parse_findings(run_result.stdout)
        if parsed is None:
            total_unparseable += 1
            logger.info(
                "Variant scan for %s on CVE #%s: no JSON findings", project.full_name, cve_report.pk
            )
            raw_outputs.append(run_result.stdout[:5000])
            continue

        findings = parsed.get("findings", []) if isinstance(parsed, dict) else []
        for finding in findings[: config.max_variants_per_cve]:
            report = _file_finding(finding, project, run_row, cve_report)
            if report is None:
                continue
            total_filed += 1
            if config.auto_verify and queue_verification(report):
                total_verified += 1
                logger.info(
                    "Queued verification for variant-scan report #%s (from CVE #%s) on %s",
                    report.pk,
                    cve_report.pk,
                    project.full_name,
                )
        raw_outputs.append(run_result.stdout[:5000])

    result.cves_examined = len(candidates)
    result.finding_count = total_filed
    result.verified_count = total_verified
    result.refused_count = total_refused
    result.failed_count = total_failed
    result.unparseable_count = total_unparseable
    result.duration_seconds = time.monotonic() - started
    result.raw_output = "\n---\n".join(raw_outputs)[:20_000]
    # An all-failed run must not look like a clean run: if every candidate
    # refused, errored, or returned unparseable output, surface it as an error
    # so the operator can tell "ran and found nothing" from "never ran right".
    if total_filed == 0 and (total_refused + total_failed + total_unparseable) == len(candidates):
        result.error = (
            f"all {len(candidates)} CVE(s) refused, failed, or unparseable — "
            "check agent credentials and the reviewer config"
        )
    logger.info("Variant scan for %s: %s", project.full_name, result.summary())
    return _finish(result)


def _build_prompt(
    *,
    project: str,
    branch: str,
    cve_title: str,
    risk_class: str,
    cve_component: str,
    introduced_commit: str,
    introduced_diff: str,
    security_model: str,
    review_guidance: str,
    magpie_skills: str,
    addendum: str,
) -> str:
    addendum_block = ""
    if addendum:
        addendum_block = ADDENDUM_TEMPLATE.format(addendum=addendum)
    return _PROMPT_TEMPLATE.format(
        project=project,
        branch=branch,
        cve_title=cve_title,
        risk_class=risk_class,
        cve_component=cve_component,
        introduced_commit=introduced_commit,
        introduced_diff=introduced_diff,
        security_model=security_model,
        review_guidance=review_guidance,
        magpie_skills=magpie_skills,
        addendum=addendum_block,
    )


def _invocation(
    reviewer: AgentCLIReviewerConfig,
    config: VariantScanConfig,
    verifier_model: str,
    prompt: str,
) -> list[str]:
    """The reviewer's argv shape, with the scan config's model and flags."""
    borrowed = reviewer.model_copy(
        update={
            "model": config.model or verifier_model or reviewer.model,
            "extra_args": [*reviewer.extra_args, *config.extra_args],
        }
    )
    return borrowed.build_invocation(prompt)


def _parse_findings(output: str) -> dict[str, Any] | None:
    """The JSON object the agent was asked for, or None when no JSON is found.

    Lenient like the introduced scan's parser: a model told to emit only JSON
    will still wrap it in prose. None so an unparseable answer is distinct from
    a clean run.
    """
    import json

    from franktheunicorn.review.backends.base import json_object_candidates

    for blob in json_object_candidates(output):
        try:
            candidate = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(candidate, dict) and "findings" in candidate:
            findings = candidate.get("findings", [])
            if isinstance(findings, list):
                candidate["findings"] = [f for f in findings if isinstance(f, dict)]
            else:
                candidate["findings"] = []
            return candidate
    return None


def _file_finding(
    finding: dict[str, Any],
    project: Project,
    run_row: VariantScanRun,
    cve_report: SecurityReport,
) -> SecurityReport | None:
    """File one variant as a SecurityReport with source="variant-scan".

    Returns the report, or None when the finding had no title or is a dup of a
    prior variant-scan report for the same parent CVE + title. The lineage to
    the source CVE lives in raw_text (not matched_cve_id — stamping an
    unconfirmed variant with the parent's CVE would make operator_has_ruled
    skip it and land it in the wrong tab).
    """
    title = str(finding.get("title", "")).strip()
    if not title:
        return None
    severity = str(finding.get("severity", "")).strip().lower() or "unknown"
    component = str(finding.get("component", "")).strip()
    paths = finding.get("suspected_paths", [])
    paths_text = (
        "\n".join(f"  - {p}" for p in paths if isinstance(p, str))
        if isinstance(paths, list)
        else ""
    )
    description = str(finding.get("description", "")).strip()
    reasoning = str(finding.get("reasoning", "")).strip()
    # Do not stamp the parent's CVE id onto an unconfirmed variant: it would
    # make operator_has_ruled skip it as "already worked" and land it in the
    # "CVE, No Branch" tab. The lineage lives in raw_text below.
    cve_id = cve_report.matched_cve_id or ""
    # Dedup: a prior variant-scan report for the same parent CVE with the same
    # title means the last run already filed this one — skip, don't re-file.
    if SecurityReport.objects.filter(
        source="variant-scan", title=title[:500], raw_text__contains=f"parent={cve_report.pk}"
    ).exists():
        return None
    body = "\n\n".join(
        part
        for part in (
            f"Component: {component}" if component else "",
            f"Severity: {severity}",
            f"Suspected paths:\n{paths_text}" if paths_text else "",
            description,
            f"Reasoning (why this is the same class of bug as the original):\n{reasoning}"
            if reasoning
            else "",
            f"Variant of: {cve_report.title} (report #{cve_report.pk}"
            + (f", {cve_id}" if cve_id else "")
            + f"; parent={cve_report.pk})",
            f"Filed by the variant-analysis scan (cadence {run_row.cadence}).",
        )
        if part
    )
    report = SecurityReport.objects.create(
        project=project,
        source="variant-scan",
        status="new",
        title=title[:500],
        claimed_severity=severity,
        parsed_component=component,
        raw_text=body,
    )
    return report
