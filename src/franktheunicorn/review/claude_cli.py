"""
Claude CLI integration.

Wraps the Anthropic ``claude`` CLI in headless prompt mode (``claude -p``)
to produce a code review. Since the CLI has no built-in PR-review
subcommand, we ask Claude to emit findings in the same
``<file>:<line> - [Severity] <title>`` block format CodeRabbit produces,
and reuse CodeRabbit's parser. Degrades gracefully when the CLI is
missing, times out, or refuses to parse.
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
from franktheunicorn.review.tool_executor import (
    DEFAULT_TIMEOUT_SECONDS,
    LocalExecutor,
    ToolExecutor,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import ClaudeCLIConfig
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
class ClaudeCLIFinding:
    """A single finding produced by the Claude CLI review."""

    file_path: str
    line_number: int | None
    severity: str
    title: str
    body: str
    suggestion: str = ""


def run_claude_cli_review(
    cwd: str,
    base_commit: str,
    config: ClaudeCLIConfig,
    executor: ToolExecutor | None = None,
) -> list[ClaudeCLIFinding]:
    """
    Run the Claude CLI against the diff between ``base_commit`` and HEAD.

    ``cwd`` must be a directory containing the project's git checkout —
    either a local path (for ``LocalExecutor``) or a remote path returned
    by ``RemoteSSHExecutor.prepare_repo``. Returns an empty list (never
    raises) when the diff is empty, the CLI is missing, the call times
    out, or the model emits unparseable output.
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
            "git diff failed in %s; skipping Claude CLI review.",
            cwd,
        )
        return []

    diff = diff_result.stdout
    if not diff.strip():
        logger.debug("Empty diff against %s; skipping Claude CLI review.", base_commit)
        return []

    if len(diff) > config.max_diff_chars:
        # Truncate at a line boundary to avoid breaking hunks mid-line.
        cutoff = diff.rfind("\n", 0, config.max_diff_chars)
        if cutoff <= 0:
            cutoff = config.max_diff_chars
        diff = diff[:cutoff] + "\n[...diff truncated...]\n"

    prompt = _PROMPT_TEMPLATE.format(diff=diff)

    cmd = [config.cli_path]
    if config.model:
        cmd += ["--model", config.model]
    cmd += list(config.extra_args)
    cmd += ["-p", prompt]

    timeout = config.timeout_seconds if config.timeout_seconds > 0 else DEFAULT_TIMEOUT_SECONDS
    result = executor.run(cmd, cwd=cwd, timeout=timeout)
    if result is None:
        return []
    if not result.ok:
        logger.error(
            "Claude CLI exited with code %d: %s",
            result.returncode,
            (result.stderr or "")[:500] or "(no stderr)",
        )
        return []

    blocks = parse_prompt_only_output(result.stdout)
    return [
        ClaudeCLIFinding(
            file_path=b.file_path,
            line_number=b.line_number,
            severity=b.severity,
            title=b.title,
            body=b.body,
            suggestion=b.suggestion,
        )
        for b in blocks
    ]


def create_drafts_from_claude_cli(
    pr: PullRequest,
    findings: list[ClaudeCLIFinding],
    project: Project | None = None,
) -> list[ReviewDraft]:
    """
    Convert Claude CLI findings into ``ReviewDraft`` rows.

    Anti-patterns gate every finding before it is persisted, just like
    the CodeRabbit path. The ``sources`` field carries attribution.
    """
    drafts: list[ReviewDraft] = []

    for finding in findings:
        matches = check_against_anti_patterns(finding.body, project)
        if matches:
            record_anti_pattern_matches(matches)
            logger.info(
                "Suppressed Claude CLI finding '%s' — matched anti-pattern(s): %s",
                finding.title,
                ", ".join(ap.pattern_text[:40] for ap in matches),
            )
            continue

        confidence = _SEVERITY_CONFIDENCE.get(finding.severity, 0.5)

        draft = ReviewDraft.objects.create(
            pull_request=pr,
            file_path=finding.file_path,
            line_number=finding.line_number,
            comment_body=finding.body,
            suggestion=finding.suggestion,
            confidence=confidence,
            status="pending",
            sources=["claude-cli"],
        )
        drafts.append(draft)

    return drafts
