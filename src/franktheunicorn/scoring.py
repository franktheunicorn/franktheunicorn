"""PR interest scoring.

Produces a float score (0.0-2.0+) that indicates how much the operator
should care about a given PR.  Higher = more interesting.

Signals:
    +1.0  operator is the PR author
    +0.8  operator was mentioned or requested as reviewer
    +0.6  PR touches a watched path
    +0.4  author is a known frequent contributor
    +0.3  new contributor (first-timer deserves attention)
    -0.3  likely AI-generated / low-context PR
    -0.1  stale PR (updated > stale_pr_days ago)
"""

from __future__ import annotations

import datetime
import fnmatch
import logging
import re

from franktheunicorn.config import OperatorConfig, ProjectConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AI-generation heuristics
# ---------------------------------------------------------------------------

# Phrases commonly found in AI-generated PRs.
_AI_PHRASES: list[str] = [
    r"i hope this (pr|pull request|helps|fix)",
    r"please let me know if (you have|there are|this is)",
    r"feel free to (ask|let me know|review|suggest)",
    r"happy to (make|update|adjust|help|provide)",
    r"lgtm",
    r"as per your (request|suggestion|feedback)",
    r"i have (updated|fixed|added|removed|changed|implemented)",
    r"this (pr|commit|change) (adds|fixes|implements|updates|removes)",
]

_AI_PATTERN = re.compile("|".join(_AI_PHRASES), re.IGNORECASE)

# Very short PR bodies are a weak signal for low-context submissions.
_LOW_CONTEXT_BODY_LEN = 80


def _paths_overlap(changed_files: list[str], watched_paths: list[str]) -> bool:
    """Return True if any changed file matches a watched path glob."""
    for pattern in watched_paths:
        for filepath in changed_files:
            if fnmatch.fnmatch(filepath, pattern) or filepath.startswith(pattern.rstrip("*/")):
                return True
    return False


def _is_likely_ai_generated(body: str) -> bool:
    """Heuristic check for AI-generated or low-context PR descriptions."""
    if len(body.strip()) < _LOW_CONTEXT_BODY_LEN:
        return True
    return bool(_AI_PATTERN.search(body))


def _is_stale(updated_at: datetime.datetime | None, stale_days: int) -> bool:
    if updated_at is None:
        return False
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=stale_days)
    return updated_at < cutoff


# ---------------------------------------------------------------------------
# Public scoring function
# ---------------------------------------------------------------------------


class ScoringContext:
    """All inputs needed to compute an interest score for one PR."""

    def __init__(
        self,
        *,
        author_login: str,
        body: str,
        labels: list[str],
        requested_reviewers: list[str],
        changed_files: list[str],
        updated_at: datetime.datetime | None,
        operator: OperatorConfig,
        project: ProjectConfig,
        mentions_in_comments: list[str],
    ) -> None:
        self.author_login = author_login
        self.body = body
        self.labels = labels
        self.requested_reviewers = requested_reviewers
        self.changed_files = changed_files
        self.updated_at = updated_at
        self.operator = operator
        self.project = project
        self.mentions_in_comments = mentions_in_comments


class ScoreResult:
    """Result of scoring a PR."""

    def __init__(self) -> None:
        self.score: float = 0.0
        self.operator_is_author: bool = False
        self.operator_mentioned: bool = False
        self.likely_ai_generated: bool = False
        self.new_contributor: bool = False
        self.signals: list[str] = []

    def add(self, delta: float, reason: str) -> None:
        self.score += delta
        self.signals.append(f"{delta:+.1f} {reason}")

    def __repr__(self) -> str:
        return f"<ScoreResult score={self.score:.2f} signals={self.signals!r}>"


def score_pr(ctx: ScoringContext) -> ScoreResult:
    """Compute an interest score for a pull request.

    Returns a ScoreResult with the total score and contributing signals.
    """
    result = ScoreResult()
    op_login = ctx.operator.github_login.lower()
    author = ctx.author_login.lower()

    # Signal: operator is the author.
    if author == op_login:
        result.operator_is_author = True
        result.add(1.0, "operator is author")

    # Signal: operator mentioned or review-requested.
    reviewer_logins = [r.lower() for r in ctx.requested_reviewers]
    mention_logins = [m.lower() for m in ctx.mentions_in_comments]
    if op_login in reviewer_logins or op_login in mention_logins:
        result.operator_mentioned = True
        result.add(0.8, "operator mentioned/requested")

    # Signal: PR touches a watched path.
    if _paths_overlap(ctx.changed_files, ctx.project.watched_paths):
        result.add(0.6, "touches watched path")

    # Signal: author is a frequent contributor.
    freq_contribs = [c.lower() for c in ctx.project.frequent_contributors]
    trusted = [c.lower() for c in ctx.operator.trusted_collaborators]
    if author in freq_contribs or author in trusted:
        result.add(0.4, "frequent/trusted contributor")

    # Signal: new contributor.
    all_known = set(freq_contribs) | set(trusted) | {op_login}
    if author not in all_known:
        result.new_contributor = True
        result.add(0.3, "new contributor")

    # Signal: likely AI-generated / low-context.
    if _is_likely_ai_generated(ctx.body):
        result.likely_ai_generated = True
        result.add(-0.3, "likely AI-generated or low-context")

    # Signal: stale PR.
    if _is_stale(ctx.updated_at, ctx.operator.stale_pr_days):
        result.add(-0.1, "stale PR")

    # Clamp to zero minimum.
    result.score = max(0.0, result.score)
    logger.debug("Scored PR by %s: %s", ctx.author_login, result)
    return result
