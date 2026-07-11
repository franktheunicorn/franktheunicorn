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
        logger.debug(
            "git diff failed in %s; skipping %s review.",
            cwd,
            config.name,
        )
        return []

    diff = diff_result.stdout
    if not diff.strip():
        logger.debug("Empty diff against %s; skipping %s review.", base_commit, config.name)
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
    result = executor.run(cmd, cwd=cwd, timeout=timeout)
    if result is None:
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
