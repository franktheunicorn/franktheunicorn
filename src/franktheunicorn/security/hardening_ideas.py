"""Mine ordinary hardening / improvement ideas from CVE-carrying reports.

A button press walks reports that name a CVE (or matched one) and have not
been mined yet, puts a coding agent in the checkout, and asks for non-security
ideas worth filing the normal way — plus whether each still exists in the
tree. Ideas land as :class:`HardeningIdea` rows; each scanned report is
stamped so a re-press does not re-issue.

Sibling of :mod:`patch_eval` (one report's proposed patch) but bulk-shaped:
many reports, many ideas, a worklist to churn through. Borrows the verifier's
reviewer and checkout helpers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db.models import Q, QuerySet
from django.utils import timezone

from franktheunicorn.core.models import HardeningIdea, Project, SecurityReport
from franktheunicorn.magpie.skills import skills_block_for_project
from franktheunicorn.review.backends.base import json_object_candidates
from franktheunicorn.security.agent_source import SCOPE_ALL, scope_q, scope_suffix
from franktheunicorn.security.hardening_ideas_prompt import (
    ADDENDUM_TEMPLATE,
    CONFIRM_TEMPLATE,
    PATCH_BLOCK,
)
from franktheunicorn.security.hardening_ideas_prompt import (
    TEMPLATE as _PROMPT_TEMPLATE,
)
from franktheunicorn.security.verifier import (
    _checkout,
    _default_branch,
    _local_checkout,
    injection_hits,
    refresh_from_upstream,
    resolve_verifier_reviewer,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import (
        AgentCLIReviewerConfig,
        HardeningIdeasConfig,
        OperatorConfig,
    )

logger = logging.getLogger(__name__)

_MAX_PATCH_CHARS = 20_000
_VALID_CATEGORIES = frozenset({"hardening", "validation", "correctness", "api", "docs", "other"})
_VALID_SEVERITIES = frozenset({"major", "important", "minor"})
_VALID_EXISTS = frozenset({"yes", "no", "unclear"})
_SECURITY_WORDS = re.compile(
    r"\b(securit(?:y|ies)|vulnerabilit(?:y|ies)|exploit(?:s|ation)?|cve-?\d)\b",
    re.IGNORECASE,
)


@dataclass
class IdeaDraft:
    """One idea the agent proposed, before persistence."""

    title: str
    description: str
    category: str = "hardening"
    severity: str = "minor"
    file_path: str = ""
    still_exists: str = "yes"
    still_exists_evidence: str = ""
    draft_ticket_title: str = ""
    draft_ticket_body: str = ""


@dataclass
class MineRun:
    """Outcome of mining one report (or declining to)."""

    ideas_created: int = 0
    ideas_skipped_dup: int = 0
    ideas_skipped_gone: int = 0
    summary: str = ""
    raw_output: str = ""
    branch: str = ""
    commit: str = ""
    agent: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    injection_hits: list[str] = field(default_factory=list)
    stale_warning: str = ""
    stamped: bool = False

    def summary_line(self) -> str:
        if self.error:
            return f"hardening mine: did not run — {self.error}"
        return (
            f"hardening mine: created={self.ideas_created} "
            f"dup={self.ideas_skipped_dup} gone={self.ideas_skipped_gone} "
            f"branch={self.branch or '-'}"
        )


@dataclass
class SweepResult:
    """Outcome of a bulk mine across unscanned CVE reports."""

    reports_examined: int = 0
    ideas_created: int = 0
    errors: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        err = f"; errors={len(self.errors)}" if self.errors else ""
        return (
            f"hardening sweep: examined={self.reports_examined} created={self.ideas_created}{err}"
        )


def cve_report_candidates(agent_scope: str = SCOPE_ALL) -> QuerySet[SecurityReport]:
    """CVE-carrying reports not yet mined, newest first.

    A report counts as CVE-carrying when it has a matched id or any NVD match.
    Closed / invalid / duplicate rows are skipped — those are done; the mine
    is for the live backlog's leftovers.
    """
    qs = (
        SecurityReport.objects.filter(hardening_scanned_at__isnull=True)
        .filter(Q(matched_cve_id__gt="") | ~Q(cve_matches=[]))
        .exclude(status__in=("closed", "invalid", "duplicate"))
        .filter(project__isnull=False)
        .filter(scope_q(agent_scope))
        .select_related("project")
        .order_by("-created_at", "-pk")
    )
    return qs


def idea_fingerprint(project_id: int, title: str, file_path: str) -> str:
    """Stable dedup key for an idea against a project."""
    norm = " ".join((title or "").lower().split())
    path = (file_path or "").strip().lower()
    raw = f"{project_id}\n{norm}\n{path}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _scrub_security_words(text: str) -> str:
    """Drop security-saying tokens from ticket drafts the agent still leaked."""
    return _SECURITY_WORDS.sub("issue", text or "").strip()


def _parse_ideas(output: str) -> tuple[str, list[IdeaDraft]]:
    """Pull summary + idea drafts from the agent's JSON, or empty on failure."""
    for blob in json_object_candidates(output):
        try:
            candidate = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(candidate, dict) or "ideas" not in candidate:
            continue
        raw_ideas = candidate.get("ideas")
        if not isinstance(raw_ideas, list):
            continue
        summary = str(candidate.get("summary") or "").strip()
        drafts: list[IdeaDraft] = []
        for item in raw_ideas:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            category = str(item.get("category") or "hardening").strip().lower()
            if category not in _VALID_CATEGORIES:
                category = "other"
            severity = str(item.get("severity") or "minor").strip().lower()
            if severity not in _VALID_SEVERITIES:
                severity = "minor"
            exists = str(item.get("still_exists") or "yes").strip().lower()
            if exists not in _VALID_EXISTS:
                exists = "unclear"
            drafts.append(
                IdeaDraft(
                    title=title[:500],
                    description=str(item.get("description") or "").strip(),
                    category=category,
                    severity=severity,
                    file_path=str(item.get("file_path") or "").strip()[:500],
                    still_exists=exists,
                    still_exists_evidence=str(item.get("still_exists_evidence") or "").strip(),
                    draft_ticket_title=_scrub_security_words(
                        str(item.get("draft_ticket_title") or title)
                    )[:200],
                    draft_ticket_body=_scrub_security_words(
                        str(item.get("draft_ticket_body") or "")
                    ),
                )
            )
        return summary, drafts
    return "", []


def _build_prompt(
    report: SecurityReport,
    branch: str,
    config: HardeningIdeasConfig,
    magpie_skills: str = "",
) -> str:
    body = report.raw_text or ""
    if len(body) > config.max_report_chars:
        body = body[: config.max_report_chars] + "\n…[truncated]"
    patch_block = ""
    if report.proposed_patch:
        patch = report.proposed_patch
        if len(patch) > _MAX_PATCH_CHARS:
            patch = patch[:_MAX_PATCH_CHARS] + "\n…[truncated]"
        patch_block = PATCH_BLOCK.format(patch=patch)
    addendum = ""
    if config.prompt_addendum:
        addendum = ADDENDUM_TEMPLATE.format(addendum=config.prompt_addendum)
    project = report.project.full_name if report.project else "(no project)"
    return _PROMPT_TEMPLATE.format(
        project=project,
        branch=branch,
        title=report.title or "(none)",
        component=report.parsed_component or "(unknown)",
        cve_id=report.matched_cve_id or "(none matched)",
        severity=report.claimed_severity or report.assessed_severity or "(unrated)",
        status=report.status,
        body=body,
        patch_block=patch_block,
        addendum=addendum,
        magpie_skills=magpie_skills,
    )


def _invocation(
    reviewer: AgentCLIReviewerConfig,
    config: HardeningIdeasConfig,
    verifier_model: str,
    prompt: str,
) -> list[str]:
    borrowed = reviewer.model_copy(
        update={
            "model": config.model or verifier_model or reviewer.model,
            "extra_args": [*reviewer.extra_args, *config.extra_args],
        }
    )
    return borrowed.build_invocation(prompt)


def _persist_ideas(report: SecurityReport, drafts: list[IdeaDraft], run: MineRun) -> None:
    """Create HardeningIdea rows; stamp the report either way."""
    assert report.project_id is not None
    project = report.project
    assert project is not None
    now = timezone.now()
    created = 0
    skipped_dup = 0
    skipped_gone = 0
    for draft in drafts:
        if draft.still_exists == "no":
            skipped_gone += 1
            continue
        fp = idea_fingerprint(project.pk, draft.title, draft.file_path)
        if HardeningIdea.objects.filter(project=project, fingerprint=fp).exists():
            skipped_dup += 1
            continue
        status = (
            HardeningIdea.STATUS_CONFIRMED
            if draft.still_exists == "yes"
            else HardeningIdea.STATUS_NEW
        )
        HardeningIdea.objects.create(
            project=project,
            source_report=report,
            fingerprint=fp,
            title=draft.title,
            description=draft.description,
            category=draft.category,
            severity=draft.severity,
            file_path=draft.file_path,
            draft_ticket_title=draft.draft_ticket_title or draft.title[:200],
            draft_ticket_body=draft.draft_ticket_body,
            still_exists=draft.still_exists,
            still_exists_evidence=draft.still_exists_evidence,
            still_exists_checked_at=now,
            status=status,
        )
        created += 1
    report.hardening_scanned_at = now
    report.hardening_idea_count = created
    report.save(update_fields=["hardening_scanned_at", "hardening_idea_count", "updated_at"])
    run.ideas_created = created
    run.ideas_skipped_dup = skipped_dup
    run.ideas_skipped_gone = skipped_gone
    run.stamped = True


def mine_report(report: SecurityReport, operator_config: OperatorConfig) -> MineRun:
    """Mine hardening ideas from one CVE report. Never raises."""
    from franktheunicorn.review.tool_executor import make_executor

    run = MineRun()
    triage = operator_config.security_triage
    verifier = triage.verifier
    config = triage.hardening_ideas
    if not config.enabled:
        run.error = "security_triage.hardening_ideas.enabled is false"
        logger.info("Hardening mine skipped: %s", run.error)
        return run
    if report.hardening_scanned_at is not None:
        run.error = "report already mined for hardening ideas"
        logger.info("Hardening mine skipped for report #%s: %s", report.pk, run.error)
        return run
    if report.project is None:
        run.error = "the report has no project, so there is no repo to review against"
        return run
    if not (report.matched_cve_id or report.cve_matches):
        run.error = "report has no CVE association — mine is for CVE-carrying reports"
        return run

    reviewer = resolve_verifier_reviewer(operator_config, verifier)
    if reviewer is None:
        run.error = f"no agent_cli_reviewers entry named {verifier.reviewer!r}"
        return run

    run.injection_hits = injection_hits(report)
    if run.injection_hits:
        named = ", ".join(sorted(set(run.injection_hits)))
        if verifier.refuse_on_injection:
            run.error = (
                f"the report contains prompt-injection patterns ({named}) and "
                "security_triage.verifier.refuse_on_injection is true"
            )
            return run
        logger.warning(
            "Report #%s contains prompt-injection patterns (%s); mining anyway.",
            report.pk,
            named,
        )

    executor = make_executor(reviewer.remote)
    cwd = executor.prepare_repo(
        report.project.owner,
        report.project.repo,
        local_path=_local_checkout(report.project.owner, report.project.repo),
        workspace_subdir=config.workspace_subdir,
    )
    if not cwd:
        run.error = "no checkout could be prepared"
        return run

    run.stale_warning = refresh_from_upstream(executor, cwd)
    branch = _default_branch(executor, cwd)
    if not branch:
        run.error = "no default branch could be resolved in the checkout"
        return run
    run.branch = branch

    from franktheunicorn.review.tool_executor import (
        agent_stderr,
        guard_agent_argv,
        guard_refusal,
    )

    commit = _checkout(executor, cwd, branch, fresh=config.fresh_worktree)
    if not commit:
        run.error = f"could not check out origin/{branch}"
        return run
    run.commit = commit

    magpie_skills = skills_block_for_project(report.project, inject_into="review")
    prompt = _build_prompt(report, branch, config, magpie_skills)
    argv = [*reviewer.cli_argv, *_invocation(reviewer, config, verifier.model, prompt)]

    started = time.monotonic()
    run.agent = reviewer.name
    result = executor.run(guard_agent_argv(argv), cwd=cwd, timeout=config.timeout_seconds)
    run.duration_seconds = time.monotonic() - started

    # Agent/infra failures leave the report unstamped so the next press retries.
    # Only a parseable answer (including an empty ideas list) stamps.
    if result is None:
        run.error = f"agent produced no result within {config.timeout_seconds}s"
        return run
    if refused := guard_refusal(result.returncode, result.stderr):
        run.error = f"agent did not run: {refused}"
        return run
    run.raw_output = (result.stdout or "")[:20_000]
    if not result.ok and not run.raw_output.strip():
        detail = (agent_stderr(result.stderr or "") or "").strip()[:300]
        run.error = f"agent exited {result.returncode}: {detail}"
        return run

    summary, drafts = _parse_ideas(run.raw_output)
    run.summary = summary
    if not summary and not drafts and run.raw_output.strip():
        run.error = "agent output was not parseable as a hardening-ideas JSON object"
        return run

    _persist_ideas(report, drafts, run)
    logger.info("Hardening mine for report #%s: %s", report.pk, run.summary_line())
    return run


def mine_backlog(
    operator_config: OperatorConfig,
    *,
    agent_scope: str = SCOPE_ALL,
) -> SweepResult:
    """Mine unscanned CVE reports up to ``max_reports_per_run``."""
    config = operator_config.security_triage.hardening_ideas
    result = SweepResult()
    if not config.enabled:
        result.errors.append("security_triage.hardening_ideas.enabled is false")
        return result

    candidates = list(cve_report_candidates(agent_scope)[: config.max_reports_per_run])
    if not candidates:
        logger.info(
            "Hardening sweep%s: nothing unscanned",
            scope_suffix(agent_scope),
        )
        return result

    for report in candidates:
        run = mine_report(report, operator_config)
        result.reports_examined += 1
        result.ideas_created += run.ideas_created
        if run.error and not run.stamped:
            result.errors.append(f"report #{report.pk}: {run.error}")
    logger.info("%s%s", result.summary_line(), scope_suffix(agent_scope))
    return result


def confirm_idea(idea: HardeningIdea, operator_config: OperatorConfig) -> str:
    """Re-check whether *idea* still exists. Returns a one-line note."""
    from franktheunicorn.review.tool_executor import make_executor

    triage = operator_config.security_triage
    verifier = triage.verifier
    config = triage.hardening_ideas
    if not config.enabled:
        return "hardening_ideas disabled"
    reviewer = resolve_verifier_reviewer(operator_config, verifier)
    if reviewer is None:
        return f"no agent_cli_reviewers entry named {verifier.reviewer!r}"

    project = idea.project
    executor = make_executor(reviewer.remote)
    cwd = executor.prepare_repo(
        project.owner,
        project.repo,
        local_path=_local_checkout(project.owner, project.repo),
        workspace_subdir=config.workspace_subdir,
    )
    if not cwd:
        return "no checkout could be prepared"
    refresh_from_upstream(executor, cwd)
    branch = _default_branch(executor, cwd)
    if not branch:
        return "no default branch"
    commit = _checkout(executor, cwd, branch, fresh=config.fresh_worktree)
    if not commit:
        return f"could not check out origin/{branch}"

    from franktheunicorn.review.tool_executor import guard_agent_argv

    prompt = CONFIRM_TEMPLATE.format(
        project=project.full_name,
        branch=branch,
        title=idea.title,
        file_path=idea.file_path or "(none)",
        description=idea.description or "(none)",
    )
    argv = [*reviewer.cli_argv, *_invocation(reviewer, config, verifier.model, prompt)]
    result = executor.run(guard_agent_argv(argv), cwd=cwd, timeout=config.timeout_seconds)
    if result is None:
        return "agent produced no result"
    output = result.stdout or ""
    exists = "unclear"
    evidence = ""
    for blob in json_object_candidates(output):
        try:
            candidate = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(candidate, dict) and "still_exists" in candidate:
            raw = str(candidate.get("still_exists") or "").strip().lower()
            if raw in _VALID_EXISTS:
                exists = raw
            evidence = str(candidate.get("evidence") or candidate.get("summary") or "").strip()
            break

    idea.still_exists = exists
    idea.still_exists_evidence = evidence
    idea.still_exists_checked_at = timezone.now()
    updates = ["still_exists", "still_exists_evidence", "still_exists_checked_at", "updated_at"]
    if exists == "yes" and idea.status in (
        HardeningIdea.STATUS_NEW,
        HardeningIdea.STATUS_DISMISSED,
    ):
        # Re-confirm can undo a prior false-negative dismiss.
        idea.status = HardeningIdea.STATUS_CONFIRMED
        updates.append("status")
    elif exists == "no" and idea.status in (
        HardeningIdea.STATUS_NEW,
        HardeningIdea.STATUS_CONFIRMED,
    ):
        idea.status = HardeningIdea.STATUS_DISMISSED
        updates.append("status")
    idea.save(update_fields=updates)
    return f"still_exists={exists}"


def projects_with_cve_candidates(agent_scope: str = SCOPE_ALL) -> list[Project]:
    """Projects that have at least one unscanned CVE report in *agent_scope*."""
    project_ids = cve_report_candidates(agent_scope).values_list("project_id", flat=True).distinct()
    return list(Project.objects.filter(pk__in=project_ids).order_by("owner", "repo"))
