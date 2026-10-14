"""Pure scoring signal functions (§2.1). No Django imports.

Weights are integer points on a 0-100 scale, normalized by the orchestrator.
Each function returns int | None (points if signal fires, None to skip).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from fnmatch import fnmatch

WEIGHTS: dict[str, int] = {
    "path_overlap": 30,
    "mentioned_or_assigned": 25,
    "has_review_request": 20,
    "recently_updated": 20,
    "prior_review_history": 15,
    "collaborator": 15,
    "touches_operator_code": 20,
    "merge_conflict": -15,
    "new_human_contributor": 10,
    "keyword_match": 10,
    "ai_generated": 10,
    "llm_interest": 20,
    "committer_is_on_it": -25,
    "updated_since_operator_review": 25,
    "pending_response": 20,
    "author_awaiting_maintainer": 20,
    "downstream_impact": 20,
    "sentry_errors": 15,
    "cve_file_history": 25,
    "draft_findings": 5,
    "jira_linked": 5,
    "jira_priority_author_created": 15,
    "jira_priority_author_commented": 3,
    "mailing_list_mention": 15,
    "mailing_list_blame_author": 10,
}

MAX_SCORE: int = 100

BOT_PATTERNS: list[str] = [
    r".*\[bot\]$",
    r"^dependabot$",
    r"^renovate$",
    r"^greenkeeper$",
]

LARGE_PR_THRESHOLD: int = 500


def _lowered(items: list[str]) -> set[str]:
    return {s.lower() for s in items}


def _identity_variants(raw: str) -> set[str]:
    """Build comparable identity forms from a display name or username."""
    folded = " ".join(raw.lower().split())
    if not folded:
        return set()
    compact = re.sub(r"[^a-z0-9]+", "", folded)
    out = {folded}
    if compact:
        out.add(compact)
    return out


def _matches_identity(raw: str, wanted: set[str]) -> bool:
    """Check whether *raw* matches one of the normalized wanted identities."""
    if not raw or not wanted:
        return False
    return any(variant in wanted for variant in _identity_variants(raw))


#: GitHub ``author_association`` values that mean "has committed here before".
#: The forge computes this per repository, so it is right where frank's own
#: known-authors list is not: GitHub's contributors listing stops at 500
#: logins, and on a Spark-sized repo everyone past that with no PR in frank's
#: database used to read as new. Apache Magpie's mentoring-welcome
#: first-time detection reads the same field (``first-time-detection.md`` and
#: ``tools/contributor-metrics`` at apache/magpie commit
#: dadb315caa8b881fc1d2195bef03223dac25915a).
KNOWN_ASSOCIATIONS: frozenset[str] = frozenset({"CONTRIBUTOR", "COLLABORATOR", "MEMBER", "OWNER"})
#: Values that mean "has not committed here", which is positive evidence of a
#: newcomer even when the contributors listing could not be fetched.
FIRST_TIME_ASSOCIATIONS: frozenset[str] = frozenset({"FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"})


def association_says_new(association: str) -> bool | None:
    """True / False when the forge's author association decides it; None when it
    does not (empty: Gitea, GitLab, a scraped row; ``NONE``; anything unknown),
    which leaves the decision to frank's known-authors rule."""
    value = (association or "").upper()
    if value in KNOWN_ASSOCIATIONS:
        return False
    if value in FIRST_TIME_ASSOCIATIONS:
        return True
    return None


def is_likely_bot(author: str) -> bool:
    """Check if an author looks like a bot account."""
    return any(re.match(p, author.lower()) for p in BOT_PATTERNS)


def is_ai_agent(author: str, ai_agents: list[str]) -> bool:
    """Check if author matches configured AI agent patterns or bot heuristics."""
    if is_likely_bot(author):
        return True
    return author.lower() in _lowered(ai_agents) if ai_agents else False


def path_matches(file_path: str, pattern: str) -> bool:
    """Check if a file path matches a watch pattern (glob or prefix).

    Supports both glob patterns (``python/pyspark/**``) and simple prefix
    patterns (``sql/catalyst/``).  If the pattern contains glob characters
    (``*``, ``?``, ``[``), :func:`fnmatch` is used; otherwise a prefix
    check is performed for backward compatibility with existing configs.
    """
    if any(c in pattern for c in "*?["):
        return fnmatch(file_path, pattern)
    return file_path.startswith(pattern)


def path_overlap_fraction(changed_files: list[str], watched_paths: list[str]) -> float:
    """Fraction of changed_files matching any watched-path pattern (0.0-1.0)."""
    if not changed_files:
        return 0.0
    matches = sum(1 for f in changed_files if any(path_matches(f, wp) for wp in watched_paths))
    return matches / len(changed_files)


def score_path_overlap(changed_files: list[str], watched_paths: list[str]) -> int | None:
    if not watched_paths or not changed_files:
        return None
    overlap = path_overlap_fraction(changed_files, watched_paths)
    return round(WEIGHTS["path_overlap"] * overlap) if overlap > 0 else None


def score_mentioned_or_assigned(
    body: str,
    assignees: list[str],
    operator_username: str,
    comment_bodies: list[str] | None = None,
) -> int | None:
    """Operator @-mentioned in PR body, a comment, or listed as assignee."""
    op = operator_username.lower()
    if op in _lowered(assignees or []):
        return WEIGHTS["mentioned_or_assigned"]
    pattern = re.compile(rf"@{re.escape(operator_username)}\b", re.IGNORECASE)
    if pattern.search(body or ""):
        return WEIGHTS["mentioned_or_assigned"]
    if comment_bodies:
        for cb in comment_bodies:
            if pattern.search(cb or ""):
                return WEIGHTS["mentioned_or_assigned"]
    return None


def score_has_review_request(
    requested_reviewers: list[str],
    operator_username: str,
) -> int | None:
    """Explicit GitHub review request for the operator."""
    if operator_username.lower() in _lowered(requested_reviewers or []):
        return WEIGHTS["has_review_request"]
    return None


def is_operator_involved(
    body: str,
    assignees: list[str],
    requested_reviewers: list[str],
    operator_username: str,
) -> bool:
    """Shared operator-involvement predicate.

    True when the operator is a requested reviewer, an assignee, or
    @-mentioned in the PR body. This is the single predicate behind both the
    "Mentioned" dashboard queue (:func:`compute_moderation_flags`) and the
    default review-gating policy (``worker.runner._should_auto_review``), so
    the two never drift apart.

    Deliberately does *not* include authorship — an operator-authored PR is a
    separate concept (routed to "Your PRs"). Callers that also want to treat
    authored PRs as "involved" check the author separately. Comment mentions
    are excluded too: this keys off the PR body + assignees + review requests
    only, not off arbitrary commenters @-ing the operator.

    An empty ``operator_username`` returns ``False`` — involvement cannot be
    determined without knowing who the operator is.
    """
    if not operator_username:
        return False
    if score_has_review_request(requested_reviewers, operator_username) is not None:
        return True
    return score_mentioned_or_assigned(body, assignees, operator_username) is not None


def score_prior_review_history(
    author: str,
    operator_username: str,
    review_history: list[dict[str, str]],
) -> int | None:
    """One-directional: has operator previously reviewed this author's PRs?"""
    if not review_history:
        return None
    a, o = author.lower(), operator_username.lower()
    reviewed = any(
        e.get("author", "").lower() == a and e.get("reviewer", "").lower() == o
        for e in review_history
    )
    return WEIGHTS["prior_review_history"] if reviewed else None


def score_new_human_contributor(
    author: str,
    operator_username: str,
    known_authors: list[str],
    ai_agents: list[str] | None = None,
    author_association: str = "",
) -> int | None:
    """New contributor bump. Excludes bots, AI agents, known authors, the
    operator, and anyone the forge says has committed here before."""
    author_lower = author.lower()
    if (
        is_ai_agent(author, ai_agents or [])
        or author_lower == operator_username.lower()
        or author_lower in _lowered(known_authors)
        or association_says_new(author_association) is False
    ):
        return None
    return WEIGHTS["new_human_contributor"]


def score_keyword_match(title: str, body: str, keywords: list[str]) -> int | None:
    """Case-insensitive keyword match in PR title or body."""
    if not keywords:
        return None
    text = f"{title}\n{body}".lower()
    if any(kw.lower() in text for kw in keywords):
        return WEIGHTS["keyword_match"]
    return None


def score_ai_generated(author: str, ai_agents: list[str] | None = None) -> int | None:
    """Boost when PR author is a bot or configured AI agent (routes to AI queue for extra review)."""
    if is_ai_agent(author, ai_agents or []):
        return WEIGHTS["ai_generated"]
    return None


def score_committer_is_on_it(
    recent_reviews: list[dict[str, str]],
    operator_username: str,
    committers: list[str],
    watched_paths: list[str],
    changed_files: list[str],
    mentioned_or_assigned: bool = False,
) -> int | None:
    """Down-rank PRs where another committer is actively reviewing (§2.7).

    Conditions for down-ranking (all must be true):
    - A known committer (not operator) appears in ``recent_reviews`` — the
      caller is responsible for pre-filtering entries to a recency window
    - PR is NOT in operator's watch_paths
    - Operator is NOT mentioned or assigned
    """
    if mentioned_or_assigned:
        return None

    # Check if PR touches watched paths — if so, don't derank
    if (
        watched_paths
        and changed_files
        and any(path_matches(f, wp) for f in changed_files for wp in watched_paths)
    ):
        return None

    committer_set = _lowered(committers)
    op = operator_username.lower()
    committer_set.discard(op)

    if not committer_set:
        return None

    has_active_committer = any(
        e.get("reviewer", "").lower() in committer_set for e in recent_reviews
    )

    return WEIGHTS["committer_is_on_it"] if has_active_committer else None


def score_recently_updated(
    hours_since_update: float | None,
) -> int | None:
    """Boost for recently updated PRs. +20 if updated today, +10 if this week."""
    if hours_since_update is None:
        return None
    today_boost = WEIGHTS["recently_updated"]
    week_boost = today_boost // 2
    if hours_since_update < 24:
        return today_boost
    if hours_since_update < 168:  # 7 days
        return week_boost
    return None


def score_merge_conflict(mergeable: bool | None) -> int | None:
    """Penalty when PR has merge conflicts (not mergeable)."""
    if mergeable is None:
        return None  # unknown status, don't penalize
    return WEIGHTS["merge_conflict"] if not mergeable else None


def score_cve_file_history(
    changed_files: list[str],
    cve_affected_files: list[str],
) -> int | None:
    """Boost when PR touches files involved in past CVE/security fixes.

    Proportional to the fraction of changed files overlapping with
    CVE-affected files. Auto-detected paths use exact match; manual
    config entries with glob chars or trailing ``/`` use pattern matching.
    """
    if not changed_files or not cve_affected_files:
        return None
    cve_set = set(cve_affected_files)
    # Only use path_matches for entries that are glob/prefix patterns,
    # not exact file paths (avoids prefix false positives like
    # "src/auth.py" matching "src/auth.py.bak").
    patterns = [p for p in cve_affected_files if any(c in p for c in "*?[") or p.endswith("/")]
    matches = sum(
        1 for f in changed_files if f in cve_set or any(path_matches(f, p) for p in patterns)
    )
    if matches == 0:
        return None
    fraction = matches / len(changed_files)
    return round(WEIGHTS["cve_file_history"] * fraction)


def score_llm_interest(llm_judgment: str | None) -> int | None:
    """Convert LLM interest judgment to points. high=20, medium=10, low/None=skip."""
    if not llm_judgment:
        return None
    match llm_judgment.strip().lower():
        case "high":
            return WEIGHTS["llm_interest"]
        case "medium":
            return WEIGHTS["llm_interest"] // 2
        case _:
            return None


def _parse_iso(dt_str: str) -> datetime:
    """Parse an ISO 8601 datetime string to a timezone-aware datetime."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


_REVIEW_GRACE_PERIOD = timedelta(minutes=5)


def score_updated_since_operator_review(
    operator_review_posted_at: str | None,
    pr_updated_at: str | None,
) -> int | None:
    """Boost when PR was updated after the operator posted a review.

    Compares the operator's most recent posted review timestamp against
    the PR's github_updated_at. A 5-minute grace period prevents the
    operator's own review comment from triggering this signal (posting
    a review bumps github_updated_at).
    """
    if not operator_review_posted_at or not pr_updated_at:
        return None
    try:
        review_dt = _parse_iso(operator_review_posted_at)
        updated_dt = _parse_iso(pr_updated_at)
    except (ValueError, TypeError):
        return None
    if updated_dt > review_dt + _REVIEW_GRACE_PERIOD:
        return WEIGHTS["updated_since_operator_review"]
    return None


def score_pending_response(
    operator_review_posted_at: str | None,
    author_replies_after_review: list[str],
) -> int | None:
    """Boost when the PR author replied after the operator's review.

    author_replies_after_review is a list of ISO 8601 timestamps of
    comments by the PR author posted after the operator's last review.
    If any exist, the author is likely waiting for follow-up.
    """
    if not operator_review_posted_at or not author_replies_after_review:
        return None
    return WEIGHTS["pending_response"]


def score_author_awaiting_maintainer(
    comments: list[dict[str, str]],
    pr_author: str,
    operator_username: str,
    committers: list[str],
) -> int | None:
    """Boost when the ball is in the maintainers' court.

    The last human comment on the PR is the author's, and it @-mentions the
    operator, a committer or a team (``org/team``). Nobody with merge rights
    has spoken since, by construction, so the author is waiting on us; a stale
    sweep or a low score would read that silence as theirs. This is Apache
    Magpie's pr-triage pre-filter F5c ("author question to a maintainer: ball
    in our court", ``tools/pr-management/src/pr_management/triage/signals.py``
    and ``plugins/magpie-pr-management/skills/pr-triage/design-notes.md`` at
    apache/magpie commit dadb315caa8b881fc1d2195bef03223dac25915a), rewritten
    on frank's data rather than ported. Two differences: frank knows its
    maintainers as the operator plus ``committers``, so an unknown mentioned
    login does not count (Magpie counts it and resolves later); and it sees
    issue comments only, so a maintainer who answered with a formal review
    rather than a comment is not seen.

    ``comments`` are ``{"author", "body", "created_at"}`` in posting order.
    The operator's own PRs never fire: there the operator is the one waiting.
    """
    from franktheunicorn.backends.mentions import mention_scan

    op = operator_username.lower()
    author = pr_author.lower()
    if not author or author == op:
        return None
    human = [c for c in comments if c.get("author") and not is_likely_bot(c["author"])]
    if not human or human[-1]["author"].lower() != author:
        return None
    maintainers = _lowered(committers)
    if op:
        maintainers.add(op)
    for hit in mention_scan(human[-1].get("body", "")):
        handle = hit.handle.lower()
        if "/" in handle or handle in maintainers:
            return WEIGHTS["author_awaiting_maintainer"]
    return None


def score_draft_findings(draft_findings_count: int | None) -> int | None:
    """Light boost when the agent found possible line-level review concerns.

    Surfaces PRs the agent already has something to say about. Boolean signal —
    the count itself drives the dashboard badge, not the score magnitude.
    """
    if not draft_findings_count or draft_findings_count <= 0:
        return None
    return WEIGHTS["draft_findings"]


def _jira_has_ticket(jira_cache: dict[str, object] | None) -> bool:
    if not jira_cache:
        return False
    ticket_id = jira_cache.get("ticket_id")
    return isinstance(ticket_id, str) and bool(ticket_id.strip())


def _jira_priority_author_set(priority_authors: list[str]) -> set[str]:
    out: set[str] = set()
    for author in priority_authors:
        out.update(_identity_variants(author))
    return out


def _jira_creator_matches(jira_cache: dict[str, object] | None, wanted: set[str]) -> bool:
    if not jira_cache or not wanted:
        return False
    for key in ("creator", "creator_username", "reporter", "reporter_username"):
        raw = jira_cache.get(key)
        if isinstance(raw, str) and _matches_identity(raw, wanted):
            return True
    return False


def score_jira_linked(jira_cache: dict[str, object] | None) -> int | None:
    """Small bump when a PR is linked to a JIRA issue."""
    if not _jira_has_ticket(jira_cache):
        return None
    return WEIGHTS["jira_linked"]


def score_jira_priority_author_created(
    jira_cache: dict[str, object] | None,
    priority_authors: list[str],
) -> int | None:
    """Large bump when a tracked person created/reported the linked JIRA issue."""
    if not _jira_has_ticket(jira_cache):
        return None
    wanted = _jira_priority_author_set(priority_authors)
    if not wanted:
        return None
    return (
        WEIGHTS["jira_priority_author_created"]
        if _jira_creator_matches(jira_cache, wanted)
        else None
    )


def score_jira_priority_author_commented(
    jira_cache: dict[str, object] | None,
    priority_authors: list[str],
) -> int | None:
    """Small bump when a tracked person commented on the linked JIRA issue."""
    if not _jira_has_ticket(jira_cache):
        return None
    if jira_cache is None:
        return None
    wanted = _jira_priority_author_set(priority_authors)
    if not wanted or _jira_creator_matches(jira_cache, wanted):
        return None
    raw_comments = jira_cache.get("recent_comments", [])
    if not isinstance(raw_comments, list):
        return None
    for raw_comment in raw_comments:
        if not isinstance(raw_comment, dict):
            continue
        for key in ("author", "author_username"):
            raw = raw_comment.get(key)
            if isinstance(raw, str) and _matches_identity(raw, wanted):
                return WEIGHTS["jira_priority_author_commented"]
    return None


def score_mailing_list_mention(
    community_context_cache: dict[str, object] | None,
    pr_identifiers: set[str] | None = None,
) -> int | None:
    """Boost when a mailing list thread references this PR or its JIRA ticket.

    ``pr_identifiers`` must contain the PR's own JIRA ticket IDs and ``#<number>``.
    Returns ``None`` when ``pr_identifiers`` is not provided — cannot verify a match
    without knowing which IDs belong to this PR.
    """
    if not community_context_cache or not pr_identifiers:
        return None
    sources = community_context_cache.get("sources", [])
    if not isinstance(sources, list):
        return None
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("type") != "mailing-list":
            continue
        for thread in source.get("threads", []):
            if not isinstance(thread, dict):
                continue
            refs = thread.get("pr_references")
            if isinstance(refs, list) and any(r in pr_identifiers for r in refs):
                return WEIGHTS["mailing_list_mention"]
    return None


def score_mailing_list_blame_author(
    community_context_cache: dict[str, object] | None,
) -> int | None:
    """Boost when a blame author for the changed files appears in a mailing list thread."""
    if not community_context_cache:
        return None
    sources = community_context_cache.get("sources", [])
    if not isinstance(sources, list):
        return None
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("type") != "mailing-list":
            continue
        for thread in source.get("threads", []):
            if not isinstance(thread, dict):
                continue
            if thread.get("blame_hit"):
                return WEIGHTS["mailing_list_blame_author"]
    return None
