"""
CodeRabbit CLI integration.

Invokes `coderabbit review --prompt-only` as a subprocess, parses the output
into ReviewDraft objects attributed to CodeRabbit. Degrades gracefully when
the CLI is not installed or not configured.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from franktheunicorn.core.models import ReviewDraft
from franktheunicorn.review.antipattern import (
    check_against_anti_patterns,
    record_anti_pattern_matches,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import CodeRabbitConfig
    from franktheunicorn.core.models import Project, PullRequest
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

# Timeout for the CLI subprocess in seconds.
_CLI_TIMEOUT_SECONDS = 120

# Severity → confidence mapping.
_SEVERITY_CONFIDENCE: dict[str, float] = {
    "critical": 0.9,
    "high": 0.8,
    "medium": 0.6,
    "low": 0.4,
    "nit": 0.3,
}

# Pattern to parse the header line of a finding block.
# Expected format: "file/path.py:42 - [Severity] Title"
_HEADER_PATTERN = re.compile(
    r"^(?P<file_path>[^\s:]+):(?P<line>\d+)\s*-\s*\[(?P<severity>[^\]]+)\]\s*(?P<title>.+)$",
)


@dataclass
class CodeRabbitFinding:
    """Intermediate representation of a single CodeRabbit finding."""

    file_path: str
    line_number: int | None
    severity: str
    title: str
    body: str
    suggestion: str = ""


def run_coderabbit_review(
    repo_path: str | Path,
    base_commit: str,
    config: CodeRabbitConfig,
    executor: ToolExecutor | None = None,
) -> list[CodeRabbitFinding]:
    """
    Run ``coderabbit review --prompt-only`` and return parsed findings.

    Returns an empty list (never raises) when the CLI is missing, times out,
    or exits with an error.

    When ``executor`` is omitted the call falls through to ``subprocess.run``
    directly, preserving the historical local-only path. Pass an executor
    (e.g. ``RemoteSSHExecutor``) to run the CLI elsewhere.
    """
    cmd = [
        *config.cli_argv,
        "review",
        "--prompt-only",
        "--no-color",
        "--base-commit",
        base_commit,
        "--type",
        "committed",
        *config.extra_args,
    ]

    if executor is not None:
        exec_result = executor.run(cmd, cwd=str(repo_path), timeout=_CLI_TIMEOUT_SECONDS)
        if exec_result is None:
            return []
        if not exec_result.ok:
            logger.error(
                "CodeRabbit CLI exited with code %d: %s",
                exec_result.returncode,
                (exec_result.stderr or "")[:500] or "(no stderr)",
            )
            return []
        return parse_prompt_only_output(exec_result.stdout)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=_CLI_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        logger.warning("CodeRabbit CLI not found at '%s'; skipping review.", config.cli_path)
        return []
    except subprocess.TimeoutExpired:
        logger.warning(
            "CodeRabbit CLI timed out after %ds; skipping review.",
            _CLI_TIMEOUT_SECONDS,
        )
        return []

    if proc.returncode != 0:
        logger.error(
            "CodeRabbit CLI exited with code %d: %s",
            proc.returncode,
            proc.stderr[:500] if proc.stderr else "(no stderr)",
        )
        return []

    return parse_prompt_only_output(proc.stdout)


def parse_prompt_only_output(raw_output: str) -> list[CodeRabbitFinding]:
    """
    Parse the ``--prompt-only`` output into a list of findings.

    Findings are separated by ``=============`` lines. A clean run that ends
    with "Review completed" and no separator blocks returns an empty list.
    """
    if not raw_output or not raw_output.strip():
        return []

    blocks = re.split(r"\n=+\n", raw_output.strip())
    findings: list[CodeRabbitFinding] = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        finding = _parse_single_block(block)
        if finding is not None:
            findings.append(finding)

    return findings


def _parse_single_block(block: str) -> CodeRabbitFinding | None:
    """Parse a single ``=============``-separated block into a finding."""
    lines = block.split("\n", 1)
    header_line = lines[0].strip()

    match = _HEADER_PATTERN.match(header_line)
    if not match:
        # Short blocks are likely summary lines ("Review completed").
        if len(block) < 40:
            return None
        # Unparseable but substantial block — capture rather than drop.
        logger.debug("Could not parse CodeRabbit header: %s", header_line[:80])
        return CodeRabbitFinding(
            file_path="",
            line_number=None,
            severity="medium",
            title=header_line[:120],
            body=block,
        )

    file_path = match.group("file_path")
    line_number = int(match.group("line"))
    severity = match.group("severity").lower()
    title = match.group("title").strip()

    body_text = lines[1].strip() if len(lines) > 1 else ""

    # Split suggestion out of the body if present.
    suggestion = ""
    suggestion_match = re.split(r"\*\*Suggestion:\*\*\s*", body_text, maxsplit=1)
    if len(suggestion_match) == 2:
        body_text = suggestion_match[0].strip()
        suggestion = suggestion_match[1].strip()

    return CodeRabbitFinding(
        file_path=file_path,
        line_number=line_number,
        severity=severity,
        title=title,
        body=f"{title}\n\n{body_text}".strip() if body_text else title,
        suggestion=suggestion,
    )


def create_drafts_from_coderabbit(
    pr: PullRequest,
    findings: list[CodeRabbitFinding],
    project: Project | None = None,
    *,
    diff_source: str = "",
) -> list[ReviewDraft]:
    """
    Convert CodeRabbit findings into ``ReviewDraft`` objects.

    Each finding is checked against anti-patterns before being persisted.
    Matched findings are silently suppressed.
    """
    drafts: list[ReviewDraft] = []

    for finding in findings:
        # Anti-pattern gate.
        matches = check_against_anti_patterns(finding.body, project)
        if matches:
            record_anti_pattern_matches(matches)
            logger.info(
                "Suppressed CodeRabbit finding '%s' — matched anti-pattern(s): %s",
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
            sources=["coderabbit"],
            diff_source=diff_source,
        )
        drafts.append(draft)

    return drafts
