"""Structured feedback formatter for AI agent sessions (v1.25).

Generates markdown-formatted feedback from review findings and test results,
suitable for pasting into a Claude Code session or similar tool.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from franktheunicorn.core.models import PullRequest, ReviewDraft, TestRun


def format_feedback_markdown(
    pr: PullRequest,
    drafts: Iterable[ReviewDraft],
    test_runs: Iterable[TestRun],
    assessment: str,
    personality_name: str = "",
) -> str:
    """Format review findings and test results as structured markdown.

    Args:
        pr: The pull request being reviewed.
        drafts: Review draft findings (any status — caller decides filtering).
        test_runs: Test run results for this PR.
        assessment: One of "good", "needs-work", "reject".
        personality_name: Optional personality name for flavored headers.

    Returns:
        Structured markdown string ready for pasting into an agent session.
    """
    lines: list[str] = []

    # Assessment header
    assessment_labels = {
        "good": "Good",
        "needs-work": "Needs Work",
        "reject": "Reject",
    }
    label = assessment_labels.get(assessment, assessment)
    if personality_name:
        lines.append(f"# {personality_name}'s Review: {label}")
    else:
        lines.append(f"# Review Feedback: {label}")
    lines.append("")
    lines.append(f"**PR:** {pr.project} #{pr.number} — {pr.title}")
    lines.append(f"**Author:** {pr.author}")
    lines.append("")

    # Group findings by file
    drafts_list = list(drafts)
    if drafts_list:
        lines.append("## Findings")
        lines.append("")

        by_file: dict[str, list[ReviewDraft]] = defaultdict(list)
        for draft in drafts_list:
            key = draft.file_path or "(general)"
            by_file[key].append(draft)

        for file_path in sorted(by_file):
            lines.append(f"### `{file_path}`")
            lines.append("")
            for draft in by_file[file_path]:
                location = ""
                if draft.line_number:
                    if draft.line_end and draft.line_end != draft.line_number:
                        location = f"L{draft.line_number}-{draft.line_end}"
                    else:
                        location = f"L{draft.line_number}"

                severity = f"[{draft.severity}]" if draft.severity else ""
                header_parts = [p for p in [location, severity, draft.category] if p]
                header = " ".join(header_parts)
                if header:
                    lines.append(f"- **{header}**: {draft.comment_body}")
                else:
                    lines.append(f"- {draft.comment_body}")

                if draft.suggestion:
                    lines.append("  ```suggestion")
                    lines.append(f"  {draft.suggestion}")
                    lines.append("  ```")
            lines.append("")
    else:
        lines.append("No specific findings.")
        lines.append("")

    # Test results
    test_runs_list = list(test_runs)
    if test_runs_list:
        lines.append("## Test Verification")
        lines.append("")
        for run in test_runs_list:
            verdict = run.differential_verdict or "pending"
            lines.append(f"- **{run.run_type}**: {run.status} — verdict: {verdict}")
            if run.error_log:
                lines.append(f"  Error: {run.error_log[:200]}")
        lines.append("")

    return "\n".join(lines)
