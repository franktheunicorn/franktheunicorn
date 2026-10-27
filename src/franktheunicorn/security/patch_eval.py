"""Evaluate a proposed patch on its merits, security aside.

Third sibling of the deep verifier and the exploit verifier. The deep verifier
says whether the vulnerability is *there*; the exploit verifier says whether
the exploit *fires*; this says whether the proposed patch is a good change
*setting aside whether it fixes a security hole* — a report ruled "not a
security issue" can still carry a patch worth landing as an ordinary improvement,
and the security path (private ticket, no PR) doesn't apply to that.

Read-only: this agent reviews the patch against the surrounding code, it does
not run anything. Single run, default branch — "is this a good change" is one
question, the per-branch version is the deep verifier's job. Also drafts a
JIRA-style ticket for the non-security version of the change (no CVE / vuln /
exploit language) — text generation, not a JIRA API call; ``jira.enabled`` is
not required.

Borrows the verifier's reviewer and checkout helpers, like the exploit
verifier. Never raises: every early return carries a reason. The verdict
vocabulary (``good`` / ``questionable`` / ``bad``) is distinct from both
siblings' so a row from each can't be read as the other.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from franktheunicorn.core.models import SecurityReport
from franktheunicorn.magpie.skills import skills_block_for_project
from franktheunicorn.review.backends.base import json_object_candidates
from franktheunicorn.security.patch_eval_prompt import (
    ADDENDUM_TEMPLATE,
)
from franktheunicorn.security.patch_eval_prompt import (
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
        OperatorConfig,
        PatchEvalConfig,
    )
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

#: Cap the patch in the prompt. Patches are usually small; a huge one is
#: almost certainly a whole-file rewrite or a vendored blob, and neither helps
#: the agent judge the change.
_MAX_PATCH_CHARS = 20_000


@dataclass
class PatchEvalRun:
    """The outcome of evaluating one report's proposed patch."""

    verdict: str = ""  # "good" | "questionable" | "bad" | "inconclusive" | "error"
    summary: str = ""
    raw_output: str = ""
    #: The substance behind the verdict — what the patch does, gets right, misses.
    patch_assessment: str = ""
    #: The drafted non-security ticket, rendered as text for the report row.
    draft_ticket: str = ""
    confidence: float | None = None
    branch: str = ""
    commit: str = ""
    agent: str = ""
    duration_seconds: float = 0.0
    #: Why the review did not run, if it didn't. Empty is the good case.
    error: str = ""
    #: Prompt-injection patterns in the report text, by name. Empty is clean.
    injection_hits: list[str] = field(default_factory=list)
    #: Why the checkout could not be refreshed from upstream, if it couldn't.
    stale_warning: str = ""

    def summary_line(self) -> str:
        """One line for the worker log and ``cmd.log``."""
        if self.error:
            return f"patch eval: did not run — {self.error}"
        parts = [f"patch eval: {self.verdict}", f"branch={self.branch}"]
        if self.confidence is not None:
            parts.append(f"confidence={self.confidence:.2f}")
        if self.stale_warning:
            parts.append("(stale checkout)")
        line = " ".join(parts)
        if self.injection_hits:
            line += (
                " NOTE: report trips prompt-injection patterns "
                f"({', '.join(sorted(set(self.injection_hits)))}) — weigh accordingly."
            )
        return line


def _build_prompt(
    report: SecurityReport,
    branch: str,
    config: PatchEvalConfig,
    magpie_skills: str = "",
) -> str:
    """The patch-evaluation prompt, with the report body and patch capped."""
    project = report.project.full_name if report.project else "(no project)"
    body = report.raw_text or ""
    if len(body) > config.max_report_chars:
        body = body[: config.max_report_chars] + "\n…[truncated]"
    patch = report.proposed_patch or ""
    if len(patch) > _MAX_PATCH_CHARS:
        patch = patch[:_MAX_PATCH_CHARS] + "\n…[truncated]"
    addendum = ""
    if config.prompt_addendum:
        addendum = ADDENDUM_TEMPLATE.format(addendum=config.prompt_addendum)
    return _PROMPT_TEMPLATE.format(
        project=project,
        branch=branch,
        title=report.title or "(none)",
        component=report.parsed_component or "(unknown)",
        severity=report.claimed_severity or "(unrated)",
        body=body,
        patch=patch,
        addendum=addendum,
        magpie_skills=magpie_skills,
    )


def _invocation(
    reviewer: AgentCLIReviewerConfig,
    config: PatchEvalConfig,
    verifier_model: str,
    prompt: str,
) -> list[str]:
    """The reviewer's argv shape, with the patch-eval config's model and flags.

    Mirrors the exploit verifier's ``_invocation``: the model falls back to the
    verifier's (not the reviewer's), so ``patch_eval.model`` empty +
    ``verifier.model`` set uses the verifier's model.
    """
    borrowed = reviewer.model_copy(
        update={
            "model": config.model or verifier_model or reviewer.model,
            "extra_args": [*reviewer.extra_args, *config.extra_args],
        }
    )
    return borrowed.build_invocation(prompt)


def _parse_verdict(output: str) -> dict[str, Any] | None:
    """The JSON object the agent was asked for, or None when no verdict is found.

    Lenient like the verifier's ``parse_verdict``: a model told to emit only JSON
    will still wrap it in prose. :func:`json_object_candidates` (last first,
    braces-in-strings aware) is reused so a balanced stray like ``{FOO}`` doesn't
    sink the parse. None rather than a default verdict so an unparseable answer
    can't pass for a considered "bad".
    """
    for blob in json_object_candidates(output):
        try:
            candidate = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(candidate, dict) and "verdict" in candidate:
            return candidate
    return None


def _render_ticket(ticket: Any) -> str:
    """Flatten the drafted ticket dict to text for the report row."""
    if not isinstance(ticket, dict):
        return ""
    title = str(ticket.get("title", "")).strip()
    desc = str(ticket.get("description", "")).strip()
    comp = str(ticket.get("component", "")).strip()
    prio = str(ticket.get("priority", "")).strip()
    parts: list[str] = []
    if title:
        parts.append(f"Title: {title}")
    if comp:
        parts.append(f"Component: {comp}")
    if prio:
        parts.append(f"Priority: {prio}")
    if desc:
        parts.append("")
        parts.append(desc)
    return "\n".join(parts)


def evaluate_patch(
    report: SecurityReport,
    operator_config: OperatorConfig,
) -> PatchEvalRun:
    """Evaluate *report*'s proposed patch against the default branch.

    Never raises: every early return carries a reason, because "the review
    produced nothing" and "the review never ran" are different facts and the
    operator has to be able to tell which they got.
    """
    from franktheunicorn.review.tool_executor import make_executor

    run = PatchEvalRun()
    triage = operator_config.security_triage
    verifier = triage.verifier
    config = triage.patch_eval
    if not config.enabled:
        run.error = "security_triage.patch_eval.enabled is false"
        logger.info("Patch evaluation skipped: %s", run.error)
        return run

    if not report.proposed_patch:
        run.error = "the report has no proposed patch, so there is nothing to evaluate"
        logger.info("Patch evaluation skipped for report #%s: %s", report.pk, run.error)
        return run

    project = report.project
    if project is None:
        run.error = "the report has no project, so there is no repo to review it against"
        logger.info("Patch evaluation skipped for report #%s: %s", report.pk, run.error)
        return run

    # Borrows the verifier's reviewer — one description of how to reach the agent
    # box, not three. A name that doesn't resolve is a config error worth saying
    # out loud, because the alternative is a patch evaluator that silently
    # never runs.
    reviewer = resolve_verifier_reviewer(operator_config, verifier)
    if reviewer is None:
        run.error = f"no agent_cli_reviewers entry named {verifier.reviewer!r}"
        return run

    # Same injection scan as the verifier and the exploit verifier, same default
    # (report rather than refuse). A report about prompt injection quotes the
    # payload, and refusing those refuses the reports an ML project most needs
    # reviewed.
    run.injection_hits = injection_hits(report)
    if run.injection_hits:
        named = ", ".join(sorted(set(run.injection_hits)))
        if verifier.refuse_on_injection:
            run.error = (
                f"the report contains prompt-injection patterns ({named}) and "
                "security_triage.verifier.refuse_on_injection is true, so it was not "
                "handed to an agent with tool access."
            )
            logger.warning("Refusing patch-eval report #%s: %s", report.pk, named)
            return run
        logger.warning(
            "Report #%s contains prompt-injection patterns (%s) and is being "
            "patch-evaluated anyway. The agent is told to treat the report as data.",
            report.pk,
            named,
        )

    executor = make_executor(reviewer.remote)
    cwd = executor.prepare_repo(
        project.owner,
        project.repo,
        local_path=_local_checkout(project.owner, project.repo),
        workspace_subdir=config.workspace_subdir,
    )
    if not cwd:
        run.error = (
            "no checkout could be prepared — for remote.mode ssh check ssh_command "
            "and remote_workspace_dir; for local mode check FRANK_REPOS_DIR"
        )
        logger.warning("Patch evaluation for report #%s: %s", report.pk, run.error)
        return run

    run.stale_warning = refresh_from_upstream(executor, cwd)
    if run.stale_warning:
        logger.warning(
            "Could not refresh %s before patch-evaluating report #%s (%s). Going "
            "ahead against whatever the checkout already had — the verdict is marked "
            "accordingly.",
            cwd,
            report.pk,
            run.stale_warning,
        )

    branch = _default_branch(executor, cwd)
    if not branch:
        run.error = "no default branch could be resolved in the checkout"
        logger.warning("Patch evaluation for report #%s: %s", report.pk, run.error)
        return run
    run.branch = branch

    logger.info("Patch-evaluating report #%s on %s", report.pk, branch)
    # Magpie skill text rides the patch-eval prompt. "review" rather than the
    # exploit verifier's "exploit": this agent judges a patch on its merits, it
    # doesn't set up preconditions or run a PoC, so the exploit-reproduction
    # skill set is the wrong reading material. Empty when nothing opts in.
    magpie_skills = skills_block_for_project(project, inject_into="review")
    _run_one(report, branch, executor, cwd, reviewer, config, verifier.model, run, magpie_skills)
    _persist(report, run)
    return run


def _run_one(
    report: SecurityReport,
    branch: str,
    executor: ToolExecutor,
    cwd: str,
    reviewer: AgentCLIReviewerConfig,
    config: PatchEvalConfig,
    verifier_model: str,
    run: PatchEvalRun,
    magpie_skills: str = "",
) -> None:
    """Run the agent once on *branch* and fill *run* with the verdict."""
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

    started = time.monotonic()
    run.agent = f"{reviewer.name}/{config.model or verifier_model or reviewer.model or 'default'}"
    # fresh_worktree defaults False here (the read-only verifier's choice): this
    # agent reads source, it doesn't build or run, so a clean tree per report is
    # the cost the read-only verifier already decided not to pay.
    commit = _checkout(executor, cwd, branch, fresh=config.fresh_worktree)
    if not commit:
        run.verdict = "error"
        run.summary = f"Could not check out origin/{branch} — see the worker log."
        run.duration_seconds = time.monotonic() - started
        return
    run.commit = commit

    prompt = _build_prompt(report, branch, config, magpie_skills)
    argv = [*reviewer.cli_argv, *_invocation(reviewer, config, verifier_model, prompt)]
    result = executor.run(guard_agent_argv(argv), cwd=cwd, timeout=config.timeout_seconds)
    run.duration_seconds = time.monotonic() - started
    for note in guard_notes(result.stderr if result is not None else ""):
        logger.warning("Patch-evaluating report #%s on %s: %s", report.pk, branch, note)
    if result is not None and (refused := guard_refusal(result.returncode, result.stderr)):
        run.verdict = "error"
        run.summary = f"The {reviewer.name} CLI did not run: {refused}."
        return

    if result is None:
        run.verdict = "error"
        run.summary = (
            f"The {reviewer.name} CLI produced no result within {config.timeout_seconds}s. "
            "Check the binary exists where the executor runs, and that the timeout is "
            "long enough for a real review."
        )
        return
    if not result.ok:
        stderr = agent_stderr(result.stderr)
        detail = (stderr or result.stdout).strip()[:500]
        summary = f"The {reviewer.name} CLI exited {result.returncode}: {detail}"
        if looks_like_auth_failure(stderr):
            advice = auth_failure_advice(
                reviewer.name, reviewer.cli_argv[0], remote=reviewer.remote.mode == "ssh"
            )
            summary = f"{summary}\n\n{advice}"
        if looks_like_workspace_trust_refusal(stderr, result.stdout):
            advice = workspace_trust_advice(reviewer.name)
            summary = f"{summary}\n\n{advice}"
        run.verdict = "error"
        run.summary = summary
        run.raw_output = result.stdout[:20_000]
        return

    parsed = _parse_verdict(result.stdout)
    if parsed is None:
        logger.warning(
            "No JSON verdict in the %s output for report #%s on %s; keeping the raw text.",
            reviewer.name,
            report.pk,
            branch,
        )
        run.verdict = "inconclusive"
        run.summary = "The agent's answer had no JSON verdict in it; its raw output is below."
        run.raw_output = result.stdout[:20_000]
        return

    run.verdict = str(parsed.get("verdict", "")).strip() or "inconclusive"
    run.summary = str(parsed.get("summary", "")).strip()
    run.patch_assessment = str(parsed.get("patch_assessment", "")).strip()
    run.draft_ticket = _render_ticket(parsed.get("draft_ticket"))
    conf = parsed.get("confidence")
    if isinstance(conf, (int, float)):
        run.confidence = float(conf)


def _persist(report: SecurityReport, run: PatchEvalRun) -> None:
    """Write the verdict onto the report row, replacing any previous attempt.

    One row per report (not per branch — this is the single-question sibling),
    so a re-run overwrites. ``patch_eval_requested`` is set here, when the run
    finishes, not when it is queued: any reload re-offers the button and the
    second click was an uncaught IntegrityError before the queue door existed.
    """
    report.patch_eval_requested = True
    report.patch_eval_verdict = run.verdict
    parts: list[str] = [run.summary_line()]
    if run.summary:
        parts.append(run.summary)
    if run.patch_assessment:
        parts.append(f"Patch assessment: {run.patch_assessment}")
    if run.draft_ticket:
        parts.append("Draft ticket (non-security):")
        parts.append(run.draft_ticket)
    if run.raw_output:
        parts.append(run.raw_output)
    report.patch_eval_result = "\n\n".join(parts)
    report.patch_eval_checked_at = timezone.now()
    report.save(
        update_fields=[
            "patch_eval_requested",
            "patch_eval_verdict",
            "patch_eval_result",
            "patch_eval_checked_at",
            "updated_at",
        ]
    )
