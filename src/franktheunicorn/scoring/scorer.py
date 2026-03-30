"""
Interest scoring for pull requests.

Scores PRs based on signals relevant to the operator:
- operator is author
- operator mentioned / requested as reviewer
- path overlap with watched paths
- collaborator / frequent contributor
- new human contributor bump
- likely AI-generated / low-context flags

Returns a score (0.0-1.0) and a breakdown dict explaining the score.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest

# Weights for each scoring signal. These are intentionally simple and tunable.
WEIGHTS = {
    "operator_is_author": 0.30,
    "review_requested": 0.25,
    "path_overlap": 0.15,
    "frequent_contributor": 0.10,
    "new_contributor": 0.10,
    "ai_generated_penalty": -0.10,
    "large_pr_penalty": -0.05,
}

# Heuristic: PRs with these author patterns are likely bots/AI
BOT_PATTERNS = [
    r".*\[bot\]$",
    r"^dependabot$",
    r"^renovate$",
    r"^greenkeeper$",
]

# Heuristic: PRs above this size get a penalty (they're harder to review well)
LARGE_PR_THRESHOLD = 500  # total additions + deletions


def score_pull_request(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_username: str,
) -> tuple[float, dict[str, float]]:
    """
    Score a PR for operator interest. Returns (score, breakdown).

    Score is clamped to [0.0, 1.0].
    """
    breakdown: dict[str, float] = {}
    author = pr.author.lower()
    operator = operator_username.lower()
    known_contributors = [c.lower() for c in project_config.frequent_contributors]

    # 1. Operator is author — you always care about your own PRs
    if author == operator:
        breakdown["operator_is_author"] = WEIGHTS["operator_is_author"]

    # 2. Operator is requested reviewer
    reviewers = [r.lower() for r in pr.requested_reviewers] if pr.requested_reviewers else []
    if operator in reviewers:
        breakdown["review_requested"] = WEIGHTS["review_requested"]

    # 3. Path overlap with watched paths
    if project_config.watched_paths and pr.changed_files:
        overlap = _path_overlap_score(pr.changed_files, project_config.watched_paths)
        if overlap > 0:
            breakdown["path_overlap"] = round(WEIGHTS["path_overlap"] * overlap, 4)

    # 4. Frequent contributor
    if author in known_contributors:
        breakdown["frequent_contributor"] = WEIGHTS["frequent_contributor"]

    # 5. New contributor bump (not operator, not known, not bot, not in project history)
    is_bot = _is_likely_bot(pr.author)
    is_known = author in known_contributors
    is_operator = author == operator
    has_prior_prs = _has_prior_prs(pr)
    if not is_bot and not is_known and not is_operator and not has_prior_prs:
        breakdown["new_contributor"] = WEIGHTS["new_contributor"]

    # 6. AI-generated / bot penalty
    if is_bot:
        breakdown["ai_generated_penalty"] = WEIGHTS["ai_generated_penalty"]

    # 7. Large PR penalty
    total_changes = pr.additions + pr.deletions
    if total_changes > LARGE_PR_THRESHOLD:
        breakdown["large_pr_penalty"] = WEIGHTS["large_pr_penalty"]

    score = sum(breakdown.values())
    score = round(max(0.0, min(1.0, score)), 4)
    return score, breakdown


def _path_overlap_score(changed_files: list[str], watched_paths: list[str]) -> float:
    """
    Compute what fraction of changed files match any watched path prefix.
    Returns a value between 0.0 and 1.0.
    """
    if not changed_files:
        return 0.0
    matches = sum(1 for f in changed_files if any(f.startswith(wp) for wp in watched_paths))
    return matches / len(changed_files)


def _has_prior_prs(pr: PullRequest) -> bool:
    """Check if the author has other PRs in this project (proxy for git log presence)."""
    from franktheunicorn.core.models import PullRequest as PRModel

    return (
        PRModel.objects.filter(
            project=pr.project,
            author__iexact=pr.author,
        )
        .exclude(pk=pr.pk)
        .exists()
    )


def _is_likely_bot(author: str) -> bool:
    """Check if an author looks like a bot account."""
    return any(re.match(pattern, author.lower()) for pattern in BOT_PATTERNS)
