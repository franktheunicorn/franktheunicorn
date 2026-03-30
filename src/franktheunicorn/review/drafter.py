"""
Stub review drafter.

Returns deterministic fake draft comments so the system is testable
without an LLM. Designed to be swapped out later for real LLM providers.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from franktheunicorn.core.models import ReviewDraft

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest

# Deterministic comment templates keyed by hash bucket
_TEMPLATES = [
    "Consider adding a test for this change.",
    "This looks good overall. One minor suggestion: could the variable name be more descriptive?",
    "Nice improvement! Have you considered the edge case where the input is empty?",
    "The logic here could be simplified. Would you be open to a small refactor?",
    "This change touches a critical path — might be worth adding a comment explaining"
    " the reasoning.",
]


def draft_review(
    pr: PullRequest,
    project_config: ProjectConfig,
) -> list[ReviewDraft]:
    """
    Generate deterministic stub review comments for a PR.

    This is a fake implementation. Real LLM-based review comes later.
    The stub generates 1-2 comments per PR based on a hash of the PR data
    so output is reproducible for the same input.
    """
    drafts: list[ReviewDraft] = []
    changed_files: list[str] = pr.changed_files or ["unknown_file.py"]

    for i, file_path in enumerate(changed_files[:2]):
        # Deterministic selection based on PR number + file path
        seed = f"{pr.number}:{file_path}"
        bucket = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(_TEMPLATES)
        comment_body = _TEMPLATES[bucket]

        # Deterministic line number
        line_number = ((pr.number * 7 + i * 13) % 50) + 1

        draft = ReviewDraft.objects.create(
            pull_request=pr,
            file_path=file_path,
            line_number=line_number,
            comment_body=comment_body,
            confidence=0.5 + (bucket * 0.1),
            status="pending",
        )
        drafts.append(draft)

    return drafts
