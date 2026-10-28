"""Build review guidance from the commits that introduced past CVEs.

The learning loop's review-guidance half. Where ``security/learning.py``
distills triage guidance from operator agree/disagree feedback, this
distills *review* guidance from the diffs of commits that introduced real
CVEs — "what should a code reviewer have looked for to catch this class of
issue at review time?"

The output is a :class:`SecurityReviewGuidance` row (one active per
project) whose ``guidance_text`` is injected into the introduced-CVE scan
prompt (see :mod:`franktheunicorn.security.introduced_scan_prompt`) so the
scan that looks for new CVEs is guided by what past CVE-introducing commits
looked like.

Git for the diffs (via the verifier's executor and checkout), LLM for the
notes and the distillation — same hybrid as ``security/cve_match.py``. Never
raises: every gate that stops the build carries a reason.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from franktheunicorn.security.verifier import (
    _checkout,
    _default_branch,
    _local_checkout,
    refresh_from_upstream,
    resolve_verifier_reviewer,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig
    from franktheunicorn.core.models import Project, SecurityReport
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 120

#: The proxy backend (llm.apache.org) rate-limits aggressively: after a burst
#: of per-commit calls, the distillation call gets a 429 "Resource exhausted.
#: Please try again in 30s." returned as a 200-with-JSON-body rather than
#: raising. Cool down before the first distillation attempt so the rate
#: window slides, then retry with the delay the error message itself names.
DISTILL_COOLDOWN = 30  # seconds before the first distillation attempt
DISTILL_RETRIES = 5
DISTILL_RETRY_DELAY = 30  # seconds (matches the proxy's own "try again in 30s")

#: How many CVE-introducing commits to review in one build. Each is one git
#: show + one LLM call, so this bounds the cost of a build. The newest valid
#: reports go first — the most recent CVEs are the most relevant guidance.
DEFAULT_MAX_COMMITS = 50

#: Only valid reports carry the operator's "this is a real CVE" ruling, so
#: they are the only reports whose introducing commit is worth learning from.
_VALID_STATUS = "valid"


@dataclass
class ReviewGuidanceResult:
    """What a build made of one project's CVE-introducing commits."""

    commit_count: int = 0
    report_count: int = 0
    guidance_text: str = ""
    #: Per-commit notes, in case the caller wants to see them (dry run).
    notes: list[str] = field(default_factory=list)
    error: str = ""
    duration_seconds: float = 0.0

    def summary(self) -> str:
        if self.error:
            return f"Review guidance build did not complete: {self.error}"
        if not self.guidance_text:
            return (
                f"Review guidance build ran over {self.commit_count} commit(s) "
                "but produced no guidance."
            )
        lead = self.guidance_text.strip().splitlines()[0][:80]
        return (
            f"Review guidance built from {self.commit_count} commit(s) "
            f"across {self.report_count} report(s): {lead}..."
        )


def build_review_guidance(
    project: Project,
    operator_config: OperatorConfig,
    *,
    max_commits: int = DEFAULT_MAX_COMMITS,
    dry_run: bool = False,
) -> ReviewGuidanceResult:
    """Distill review guidance from *project*'s CVE-introducing commits.

    Never raises. Walks the project's valid reports that have a dated
    introducing commit, shows each commit's diff, asks an LLM what a reviewer
    should have caught, then distills the per-commit notes into a single
    guidance addendum. Stores it as a :class:`SecurityReviewGuidance` row
    unless ``dry_run``.
    """
    from franktheunicorn.core.models import SecurityReport, SecurityReviewGuidance
    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.tool_executor import make_executor

    started = time.monotonic()
    result = ReviewGuidanceResult()
    triage = operator_config.security_triage
    verifier = triage.verifier

    if not verifier.enabled:
        result.error = "security_triage.verifier.enabled is false"
        logger.info("Review guidance build skipped: %s", result.error)
        return result

    if not operator_config.llm_backends:
        result.error = "no llm_backends configured — guidance distillation needs an LLM"
        logger.info("Review guidance build skipped for %s: %s", project.full_name, result.error)
        return result

    reports = list(
        SecurityReport.objects.filter(project=project, status=_VALID_STATUS)
        .exclude(introduced_commit="")
        .order_by("-updated_at")[:max_commits]
    )
    if not reports:
        result.error = "no valid reports with a dated introducing commit"
        logger.info("Review guidance build skipped for %s: %s", project.full_name, result.error)
        return result

    reviewer = resolve_verifier_reviewer(operator_config, verifier)
    if reviewer is None:
        result.error = f"no agent_cli_reviewers entry named {verifier.reviewer!r}"
        logger.warning("Review guidance build for %s: %s", project.full_name, result.error)
        return result

    executor = make_executor(reviewer.remote)
    cwd = executor.prepare_repo(
        project.owner,
        project.repo,
        local_path=_local_checkout(project.owner, project.repo),
        workspace_subdir=verifier.workspace_subdir,
    )
    if not cwd:
        result.error = (
            "no checkout could be prepared — for remote.mode ssh check ssh_command "
            "and remote_workspace_dir; for local mode check FRANK_REPOS_DIR"
        )
        logger.warning("Review guidance build for %s: %s", project.full_name, result.error)
        return result

    stale = refresh_from_upstream(executor, cwd)
    if stale:
        logger.warning(
            "Could not refresh %s before building review guidance for %s (%s). "
            "Going ahead against whatever the checkout already had.",
            cwd,
            project.full_name,
            stale,
        )

    default = _default_branch(executor, cwd)
    if not default or not _checkout(executor, cwd, default):
        result.error = f"could not check out the default branch ({default or 'unknown'})"
        logger.warning("Review guidance build for %s: %s", project.full_name, result.error)
        return result

    backend = get_backend(operator_config.llm_backends[0])
    from franktheunicorn.security.review_guidance_prompt import (
        DISTILL_SYSTEM,
        PER_COMMIT_SYSTEM,
        build_distill_prompt,
        build_per_commit_prompt,
    )

    seen_commits: set[str] = set()
    notes: list[str] = []
    for report in reports:
        commit = (report.introduced_commit or "").strip()
        if not commit or commit in seen_commits:
            continue
        seen_commits.add(commit)
        diff = _show_commit(executor, cwd, commit)
        if diff is None:
            logger.info(
                "Review guidance for %s: could not show commit %s, skipping report #%s",
                project.full_name,
                commit[:12],
                report.pk,
            )
            continue
        description = _cve_description(report)
        try:
            note = backend.complete(
                build_per_commit_prompt(commit, description, diff),
                system=PER_COMMIT_SYSTEM,
            )
        except Exception:
            logger.exception(
                "Per-commit guidance call failed for %s commit %s (report #%s)",
                project.full_name,
                commit[:12],
                report.pk,
            )
            continue
        if _looks_like_backend_error(note):
            logger.warning(
                "Per-commit guidance call for %s commit %s (report #%s) returned an error, skipping",
                project.full_name,
                commit[:12],
                report.pk,
            )
            continue
        note = _extract_text(note).strip()
        if note:
            notes.append(note)
            logger.info(
                "Review guidance note for %s commit %s (report #%s): %d chars",
                project.full_name,
                commit[:12],
                report.pk,
                len(note),
            )

    result.notes = notes
    result.commit_count = len(seen_commits)
    result.report_count = len(reports)
    if not notes:
        result.error = "no per-commit notes could be produced (every call failed or was empty)"
        result.duration_seconds = time.monotonic() - started
        return result

    # The proxy backend rate-limits aggressively: after a burst of per-commit
    # calls, the distillation call gets a 429 "Resource exhausted. Please try
    # again in 30s." Cool down before the first attempt so the rate window
    # slides, then retry with the delay the error itself names. The per-commit
    # work (the expensive part) already succeeded by this point.
    logger.info(
        "Review guidance for %s: collected %d per-commit notes, cooling down %ds before distillation",
        project.full_name,
        len(notes),
        DISTILL_COOLDOWN,
    )
    time.sleep(DISTILL_COOLDOWN)
    guidance = ""
    for attempt in range(1, DISTILL_RETRIES + 1):
        try:
            raw = backend.complete(build_distill_prompt(notes), system=DISTILL_SYSTEM)
        except Exception:
            logger.exception(
                "Review guidance distillation call %d/%d failed for %s",
                attempt,
                DISTILL_RETRIES,
                project.full_name,
            )
            guidance = ""
            continue
        # Check for an error response on the RAW text before unwrapping —
        # _extract_text would strip the JSON envelope and hide the error shape.
        if _looks_like_backend_error(raw):
            logger.warning(
                "Review guidance distillation attempt %d/%d for %s returned an error response; retrying in %ds",
                attempt,
                DISTILL_RETRIES,
                project.full_name,
                DISTILL_RETRY_DELAY,
            )
            if attempt < DISTILL_RETRIES:
                time.sleep(DISTILL_RETRY_DELAY)
            continue
        guidance = _extract_text(raw).strip()
        if guidance:
            break

    if not guidance:
        result.error = "the distillation call failed after retries (rate limit or transient error)"
        result.duration_seconds = time.monotonic() - started
        return result

    result.guidance_text = guidance
    if not dry_run:
        SecurityReviewGuidance.objects.update_or_create(
            project=project,
            is_active=True,
            defaults={
                "guidance_text": guidance,
                "source_report_count": len(reports),
                "source_commit_count": len(seen_commits),
            },
        )
        logger.info(
            "Stored review guidance for %s (%d commit(s), %d report(s))",
            project.full_name,
            len(seen_commits),
            len(reports),
        )
    result.duration_seconds = time.monotonic() - started
    logger.info("Review guidance build for %s: %s", project.full_name, result.summary())
    return result


def _show_commit(executor: ToolExecutor, cwd: str, commit: str) -> str | None:
    """``git show <commit>`` — the introducing diff, capped by the caller.

    Returns the diff text, or None when git gave nothing usable. ``--no-patch``
    is NOT used: the diff is the whole point.
    """
    result = executor.run(
        ["git", "show", "--no-color", commit],
        cwd=cwd,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result is None or not result.ok:
        detail = "no result" if result is None else (result.stderr or "").strip()[:200]
        logger.info("git show %s failed: %s", commit[:12], detail)
        return None
    return str(result.stdout)


def _cve_description(report: SecurityReport) -> str:
    """A one-line description of the CVE a report represents, for the prompt."""
    cve = f"[{report.matched_cve_id}] " if report.matched_cve_id else ""
    title = (report.title or report.raw_text[:120] or "untitled").replace("\n", " ").strip()
    return f"{cve}{title}"


def _looks_like_backend_error(text: str) -> bool:
    """Heuristic: the openai-compatible backend returns an error JSON body rather
    than raising on a non-200 response. Catch the common shape so we don't store
    a rate-limit message as if it were guidance.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and (
        "error" in parsed
        or ("message" in parsed and "code" in parsed)
        or any(isinstance(v, dict) and "message" in v for v in parsed.values())
    )


def _extract_text(text: str) -> str:
    """Pull readable text out of a JSON-wrapped LLM response.

    The openai-compatible backend forces ``response_format=json_object``, so
    the model returns ``{"analysis": "..."}`` or ``{"guidance": ["...", ...]}``
    even when the prompt asked for plain bullets. Unwrap that so the stored
    guidance is plain text, not JSON. If the response isn't JSON, return it
    as-is.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return stripped
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return stripped
    if not isinstance(parsed, dict):
        return stripped
    parts: list[str] = []
    for value in parsed.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
    return "\n".join(parts) if parts else stripped


def resolve_review_guidance(project: Project | None) -> str:
    """Resolve the learned review guidance to inject for *project*.

    Precedence (mirrors :func:`security.learning.resolve_triage_guidance`):
      1. Active project-specific guidance.
      2. Active global guidance (``project=None``).
      3. Empty string.

    Read-only.
    """
    from franktheunicorn.core.models import SecurityReviewGuidance

    if project is not None:
        project_guidance = (
            SecurityReviewGuidance.objects.filter(project=project, is_active=True)
            .order_by("-updated_at")
            .first()
        )
        if project_guidance and project_guidance.guidance_text.strip():
            return project_guidance.guidance_text.strip()

    global_guidance = (
        SecurityReviewGuidance.objects.filter(project__isnull=True, is_active=True)
        .order_by("-updated_at")
        .first()
    )
    if global_guidance and global_guidance.guidance_text.strip():
        return global_guidance.guidance_text.strip()

    return ""
