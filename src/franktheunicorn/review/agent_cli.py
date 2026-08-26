"""
Generalized agent-CLI code reviewer.

Any headless coding agent that takes a prompt on the command line and
prints free-form text can act as a reviewer. We feed it the same
``<file>:<line> - [Severity] <title>`` block-format prompt CodeRabbit
produces and parse the output with the shared parser. The three seeded
agents — ``claude``, ``codex``, and ``pi`` — differ only in how a prompt
becomes argv, which is delegated to
:meth:`AgentCLIReviewerConfig.build_invocation`.

This is the generalization of ``review/claude_cli.py`` (which now delegates
here for backwards compatibility). Degrades gracefully: an empty diff, a
missing binary, a CLI error, or unparseable output all yield ``[]`` — it
never raises.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from franktheunicorn.core.models import ReviewDraft
from franktheunicorn.review.antipattern import (
    check_against_anti_patterns,
    record_anti_pattern_matches,
)
from franktheunicorn.review.coderabbit import parse_prompt_only_output
from franktheunicorn.review.dedup import is_duplicate_finding
from franktheunicorn.review.tool_executor import (
    DEFAULT_TIMEOUT_SECONDS,
    LocalExecutor,
    ToolExecutor,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import AgentCLIReviewerConfig
    from franktheunicorn.core.models import Project, PullRequest

logger = logging.getLogger(__name__)

_GIT_DIFF_TIMEOUT_SECONDS = 30

_SEVERITY_CONFIDENCE: dict[str, float] = {
    "critical": 0.9,
    "high": 0.8,
    "medium": 0.6,
    "low": 0.4,
    "nit": 0.3,
}

# Shared prompt template. General-purpose agents (claude, codex, pi) all
# receive this identical instruction; only argv assembly differs per agent.
_PROMPT_TEMPLATE = """\
You are a senior code reviewer. Review the diff below and identify substantive
issues — bugs, race conditions, security holes, API misuse, missing error
handling, or breakage of established invariants. Skip stylistic nits unless
they materially affect readability.

For EACH issue, emit a block in EXACTLY this format, separated by lines of
five or more equals signs:

<file_path>:<line_number> - [<Severity>] <Short title>

<2-4 sentence explanation of the issue>

**Suggestion:** <concrete fix>

=============

Severity must be one of: Critical, High, Medium, Low, Nit.
File paths must be relative to the repository root, exactly as they appear
in the diff. Line numbers must refer to the new file.

If there are no substantive issues, output exactly: Review completed
Do not include any other text outside the blocks.

Diff:
{diff}
"""


@dataclass
class AgentCLIFinding:
    """A single finding produced by an agent-CLI review."""

    file_path: str
    line_number: int | None
    severity: str
    title: str
    body: str
    suggestion: str = ""


def run_agent_cli_review(
    cwd: str,
    base_commit: str,
    config: AgentCLIReviewerConfig,
    executor: ToolExecutor | None = None,
) -> list[AgentCLIFinding]:
    """
    Run the agent CLI against the diff between ``base_commit`` and HEAD.

    ``cwd`` must be a directory containing the project's git checkout —
    either a local path (for ``LocalExecutor``) or a remote path returned
    by ``RemoteSSHExecutor.prepare_repo``. Returns an empty list (never
    raises) when the diff is empty, the CLI is missing, the call times
    out, or the model emits unparseable output.

    Argv assembly is delegated to ``config.build_invocation`` so the same
    body serves flag-style agents (claude, pi) and subcommand-style agents
    (codex).
    """
    if executor is None:
        executor = LocalExecutor()

    diff_result = executor.run(
        ["git", "diff", base_commit, "HEAD"],
        cwd=cwd,
        timeout=_GIT_DIFF_TIMEOUT_SECONDS,
    )
    if diff_result is None or not diff_result.ok:
        # WARNING, not DEBUG. This is a reviewer the operator explicitly enabled
        # declining to run, and at DEBUG the whole path was invisible: the symptom
        # is "claude_cli never fires" with a completely clean INFO log.
        logger.warning(
            "%s review skipped: git diff %s..HEAD failed in %s%s",
            config.name,
            base_commit,
            cwd,
            f" — {(diff_result.stderr or '').strip()[:300]}"
            if diff_result is not None
            else " — the executor returned nothing (SSH command failed or timed out)",
        )
        return []

    diff = diff_result.stdout
    if not diff.strip():
        logger.info(
            "%s review skipped: no diff between %s and HEAD in %s "
            "(the checkout may not have the PR head fetched).",
            config.name,
            base_commit,
            cwd,
        )
        return []

    if len(diff) > config.max_diff_chars:
        # Truncate at a line boundary to avoid breaking hunks mid-line.
        cutoff = diff.rfind("\n", 0, config.max_diff_chars)
        if cutoff <= 0:
            cutoff = config.max_diff_chars
        diff = diff[:cutoff] + "\n[...diff truncated...]\n"

    prompt = _PROMPT_TEMPLATE.format(diff=diff)

    cmd = list(config.cli_argv) + config.build_invocation(prompt)

    timeout = config.timeout_seconds if config.timeout_seconds > 0 else DEFAULT_TIMEOUT_SECONDS
    # The argv, once, at INFO. Without it there is no way to tell from a log
    # whether the CLI was invoked at all, let alone whether `cli_path`,
    # `prompt_mode` and `model_flag` came out the way the YAML meant them to —
    # and a wrong prompt_mode (`codex -p …` instead of `codex exec …`) fails by
    # producing no findings, which looks identical to not running.
    # The prompt is replaced by name, not sliced off by index. Slicing was wrong
    # for every reviewer whose model is unset: codex and pi ship with model="",
    # so build_invocation returns just [prompt_arg, prompt] and the window landed
    # on the prompt itself — logging the entire 60,000-char diff, private-repo
    # source and all, at INFO on every PR.
    shown = " ".join(f"<prompt: {len(prompt)} chars>" if part == prompt else part for part in cmd)
    logger.info(
        "%s review: running %s in %s (timeout %ds, %d diff chars)",
        config.name,
        shown,
        cwd,
        timeout,
        len(diff),
    )
    result = executor.run(cmd, cwd=cwd, timeout=timeout)
    if result is None:
        # The single loudest silent failure on this path: for a RemoteSSHExecutor
        # this is "the ssh_command did not come back" — a bad `ssh_command`, an
        # unreachable workspace, or the CLI hanging past the timeout — and it
        # returned an empty list with no log line whatsoever.
        logger.error(
            "%s review failed: the executor returned no result for %s. "
            "For remote.mode: ssh check that ssh_command works from this host and "
            "that %s exists on the remote; the call may also have exceeded the %ds timeout.",
            config.name,
            cmd[0],
            cwd,
            timeout,
        )
        return []
    if not result.ok:
        logger.error(
            "%s CLI exited with code %d: %s",
            config.name,
            result.returncode,
            (result.stderr or "")[:500] or "(no stderr)",
        )
        return []

    blocks = parse_prompt_only_output(result.stdout)
    if not blocks:
        # Ran fine, said nothing parseable. Distinguishing "clean review" from
        # "the model ignored the output format" needs the raw head of stdout.
        logger.info(
            "%s review returned no findings (%d bytes of output%s).",
            config.name,
            len(result.stdout or ""),
            f", starting {result.stdout.strip()[:120]!r}" if (result.stdout or "").strip() else "",
        )
    return [
        AgentCLIFinding(
            file_path=b.file_path,
            line_number=b.line_number,
            severity=b.severity,
            title=b.title,
            body=b.body,
            suggestion=b.suggestion,
        )
        for b in blocks
    ]


def create_drafts_from_agent_cli(
    pr: PullRequest,
    findings: list[AgentCLIFinding],
    project: Project | None = None,
    *,
    source: str,
    diff_source: str = "",
    deduplicate: bool = True,
) -> list[ReviewDraft]:
    """
    Convert agent-CLI findings into ``ReviewDraft`` rows, attributed to
    ``source`` (the reviewer's name, e.g. ``"claude"``/``"codex"``/``"pi"``).

    Anti-patterns gate every finding before it is persisted. When
    ``deduplicate`` is set (the default), a finding that matches an existing
    draft on the PR — same file, near line, similar body — does not create a
    second draft; instead ``source`` is appended to the existing draft's
    ``sources`` so the PR isn't spammed once per agent while attribution
    still records every reviewer that flagged the spot.
    """
    drafts: list[ReviewDraft] = []

    # Snapshot existing drafts once so cross-agent dedup compares against
    # both prior tools and agents that already ran this PR.
    existing: list[ReviewDraft] = list(pr.review_drafts.all()) if deduplicate else []

    for finding in findings:
        matches = check_against_anti_patterns(finding.body, project)
        if matches:
            record_anti_pattern_matches(matches)
            logger.info(
                "Suppressed %s finding '%s' — matched anti-pattern(s): %s",
                source,
                finding.title,
                ", ".join(ap.pattern_text[:40] for ap in matches),
            )
            continue

        if deduplicate:
            dup = _find_duplicate_draft(existing, finding)
            if dup is not None:
                if source not in dup.sources:
                    dup.sources = [*dup.sources, source]
                    dup.save(update_fields=["sources", "updated_at"])
                logger.info(
                    "Deduped %s finding '%s' into existing draft #%s (sources=%s)",
                    source,
                    finding.title,
                    dup.pk,
                    dup.sources,
                )
                continue

        confidence = _SEVERITY_CONFIDENCE.get(finding.severity, 0.5)

        from franktheunicorn.review.drafter import _coerce_severity

        draft = ReviewDraft.objects.create(
            pull_request=pr,
            file_path=finding.file_path,
            line_number=finding.line_number,
            comment_body=finding.body,
            suggestion=finding.suggestion,
            confidence=confidence,
            severity=_coerce_severity(finding.severity, finding.title),
            status="pending",
            sources=[source],
            backend_used=source,
            diff_source=diff_source,
        )
        drafts.append(draft)
        existing.append(draft)

    return drafts


def _find_duplicate_draft(
    existing: list[ReviewDraft],
    finding: AgentCLIFinding,
) -> ReviewDraft | None:
    """Return the first existing draft that duplicates ``finding``, if any."""
    for draft in existing:
        if is_duplicate_finding(
            draft.file_path,
            draft.line_number,
            draft.comment_body,
            finding.file_path,
            finding.line_number,
            finding.body,
        ):
            return draft
    return None
