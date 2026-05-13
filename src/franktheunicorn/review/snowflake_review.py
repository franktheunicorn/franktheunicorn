"""
Snowflake code review CLI integration.

Mirrors the CodeRabbit shape: invokes
``snowflake-code-review review --base-commit <sha> --prompt-only`` and
parses the same finding-block format. Degrades gracefully when the CLI
is missing or misbehaving.
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
    LocalExecutor,
    ToolExecutor,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import SnowflakeReviewConfig
    from franktheunicorn.core.models import Project, PullRequest

logger = logging.getLogger(__name__)

_CLI_TIMEOUT_SECONDS = 180

_SEVERITY_CONFIDENCE: dict[str, float] = {
    "critical": 0.9,
    "high": 0.8,
    "medium": 0.6,
    "low": 0.4,
    "nit": 0.3,
}


@dataclass
class SnowflakeFinding:
    """A single finding produced by the Snowflake review CLI."""

    file_path: str
    line_number: int | None
    severity: str
    title: str
    body: str
    suggestion: str = ""


def run_snowflake_review(
    cwd: str,
    base_commit: str,
    config: SnowflakeReviewConfig,
    executor: ToolExecutor | None = None,
) -> list[SnowflakeFinding]:
    """
    Run ``snowflake-code-review review --prompt-only`` and return findings.

    ``cwd`` is the working directory where the CLI runs — either a local
    repo path or a remote path returned by ``RemoteSSHExecutor.prepare_repo``.
    Returns an empty list on any failure.
    """
    if executor is None:
        executor = LocalExecutor()

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

    result = executor.run(cmd, cwd=cwd, timeout=_CLI_TIMEOUT_SECONDS)
    if result is None:
        return []

    if not result.ok:
        logger.error(
            "Snowflake review CLI exited with code %d: %s",
            result.returncode,
            (result.stderr or "")[:500] or "(no stderr)",
        )
        return []

    blocks = parse_prompt_only_output(result.stdout)
    return [
        SnowflakeFinding(
            file_path=b.file_path,
            line_number=b.line_number,
            severity=b.severity,
            title=b.title,
            body=b.body,
            suggestion=b.suggestion,
        )
        for b in blocks
    ]


def create_drafts_from_snowflake(
    pr: PullRequest,
    findings: list[SnowflakeFinding],
    project: Project | None = None,
    *,
    diff_source: str = "",
) -> list[ReviewDraft]:
    """
    Convert Snowflake findings into ``ReviewDraft`` rows, gated by
    anti-patterns. Source attribution is ``"snowflake-review"``.
    """
    drafts: list[ReviewDraft] = []

    for finding in findings:
        matches = check_against_anti_patterns(finding.body, project)
        if matches:
            record_anti_pattern_matches(matches)
            logger.info(
                "Suppressed Snowflake finding '%s' — matched anti-pattern(s): %s",
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
            sources=["snowflake-review"],
            diff_source=diff_source,
        )
        drafts.append(draft)

    return drafts
