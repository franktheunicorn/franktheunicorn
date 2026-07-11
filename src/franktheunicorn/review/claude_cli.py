"""
Claude CLI integration (backwards-compatibility shim).

The Claude CLI is now just one entry in the generalized agent-CLI reviewer
registry (see ``review/agent_cli.py`` and ``AgentCLIReviewerConfig``). This
module remains so existing imports and the legacy ``claude_cli:`` operator
config keep working: it adapts a :class:`ClaudeCLIConfig` into an
``AgentCLIReviewerConfig`` and delegates to the shared implementation. The
source label stays ``"claude-cli"`` for continuity with historical drafts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from franktheunicorn.review.agent_cli import (
    AgentCLIFinding,
    create_drafts_from_agent_cli,
    run_agent_cli_review,
)
from franktheunicorn.review.tool_executor import ToolExecutor

if TYPE_CHECKING:
    from franktheunicorn.config.models import AgentCLIReviewerConfig, ClaudeCLIConfig
    from franktheunicorn.core.models import Project, PullRequest, ReviewDraft

logger = logging.getLogger(__name__)

_CLAUDE_CLI_SOURCE = "claude-cli"


@dataclass
class ClaudeCLIFinding:
    """A single finding produced by the Claude CLI review."""

    file_path: str
    line_number: int | None
    severity: str
    title: str
    body: str
    suggestion: str = ""


def _adapt_config(config: ClaudeCLIConfig) -> AgentCLIReviewerConfig:
    """Adapt the legacy ``ClaudeCLIConfig`` into an agent-CLI reviewer config."""
    from franktheunicorn.config.models import AgentCLIReviewerConfig

    return AgentCLIReviewerConfig(
        name="claude",
        enabled=config.enabled,
        cli_path=config.cli_path,
        model=config.model,
        prompt_mode="flag",
        prompt_arg="-p",
        extra_args=list(config.extra_args),
        timeout_seconds=config.timeout_seconds,
        max_diff_chars=config.max_diff_chars,
        remote=config.remote,
    )


def run_claude_cli_review(
    cwd: str,
    base_commit: str,
    config: ClaudeCLIConfig,
    executor: ToolExecutor | None = None,
) -> list[ClaudeCLIFinding]:
    """Run the Claude CLI review — thin wrapper over ``run_agent_cli_review``."""
    findings = run_agent_cli_review(cwd, base_commit, _adapt_config(config), executor=executor)
    return [
        ClaudeCLIFinding(
            file_path=f.file_path,
            line_number=f.line_number,
            severity=f.severity,
            title=f.title,
            body=f.body,
            suggestion=f.suggestion,
        )
        for f in findings
    ]


def create_drafts_from_claude_cli(
    pr: PullRequest,
    findings: list[ClaudeCLIFinding],
    project: Project | None = None,
    *,
    diff_source: str = "",
) -> list[ReviewDraft]:
    """Convert Claude CLI findings into ``ReviewDraft`` rows (source ``claude-cli``)."""
    agent_findings = [
        AgentCLIFinding(
            file_path=f.file_path,
            line_number=f.line_number,
            severity=f.severity,
            title=f.title,
            body=f.body,
            suggestion=f.suggestion,
        )
        for f in findings
    ]
    return create_drafts_from_agent_cli(
        pr,
        agent_findings,
        project,
        source=_CLAUDE_CLI_SOURCE,
        diff_source=diff_source,
    )
