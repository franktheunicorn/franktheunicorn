"""Merge readiness — the "Almost Ready to Merge" queue.

Interest scoring (``scorer.py``) answers "what should I look at". This answers
a different question: "what could I merge, or LGTM, right now?" — the happy
PRs: green CI (or a failure that is probably a flake), approved or
approvable, small enough to take in at a glance, nothing serious from frank's
own review, no conflicts, not stale, not a draft.

Two halves, like the scorer:

- The pure half (no Django): :func:`classify_ci` turns a commit's check runs
  into green / flaky / red / pending, with a per-check "probably a flake"
  confidence; :func:`evaluate_merge_readiness` turns a :class:`ReadinessInput`
  into a :class:`MergeReadiness` verdict with reasons and blockers.
- The Django half: fetch reviews and checks from the forge onto the
  ``PullRequest`` row, evaluate and store the verdict, and — the part that
  matters for a queue the operator is going to disagree with — record what
  she said and learn from it.

Learning is deliberately transparent rather than a model. A dismissal with a
reason writes a :class:`MergeReadinessFeedback` row carrying the PR's
feature tokens (``author:``, ``label:``, ``path:``, ``size:``); the scorer
reads the dismissal rate per token back and docks PRs that look like the ones
she sent back, saying so in the reasons list. A reason code that keeps coming
up (three "needs tests") becomes a blocker for the next PR with that gap. And
a CI check earns or loses "probably a flake" through
:class:`~franktheunicorn.core.models.CICheckHistory`: a rebase we asked for
that went green is a confirmed flake; "the CI failure is real" is a confirmed
failure. Every rule is one line the dashboard can show.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from franktheunicorn.config.models import DEFAULT_DETERMINISTIC_CHECKS
from franktheunicorn.scoring.moderation import is_wip_title

if TYPE_CHECKING:
    from franktheunicorn.backends.base import ForgeClient
    from franktheunicorn.config.models import MergeReadinessConfig, ProjectConfig
    from franktheunicorn.core.models import MergeReadinessFeedback, Project, PullRequest

logger = logging.getLogger(__name__)

#: The dashboard tab key. Not a ``PullRequest.queue`` value — a PR keeps its
#: moderation queue (new-contributor, review, ...) and is *also* almost ready.
ALMOST_READY_QUEUE = "almost-ready"

#: ``PullRequest.merge_readiness_verdict`` values. Not ready is the empty string
#: so the common case costs nothing to store and the index filter is one IN.
VERDICT_READY = "ready"
VERDICT_FLAKY_CI = "flaky-ci"
VERDICT_NOT_READY = ""
LISTED_VERDICTS: tuple[str, ...] = (VERDICT_READY, VERDICT_FLAKY_CI)

# CI assessment statuses.
CI_GREEN = "green"
CI_FLAKY = "flaky"
CI_RED = "red"
CI_PENDING = "pending"
CI_UNKNOWN = "unknown"

#: Check-run conclusions that count as a failure. ``cancelled`` and
#: ``timed_out`` are in: they stop the merge just the same, and they are the
#: ones most likely to be nobody's fault.
FAILED_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
PASSED_CONCLUSIONS: frozenset[str] = frozenset({"success", "neutral", "skipped"})

#: A failing check is "probably a flake" at or above this confidence.
FLAKE_THRESHOLD = 0.5

# Name-based priors, before the learned history has a say.
_CONCLUSION_FLAKE_PRIOR: dict[str, float] = {
    "timed_out": 0.7,
    "cancelled": 0.6,
    "startup_failure": 0.6,
}
_CONFIGURED_FLAKY_PRIOR = 0.9
_DETERMINISTIC_PRIOR = 0.1
_DEFAULT_FLAKE_PRIOR = 0.35

#: Readiness points, 0-100 scale like the interest signals. Positive is
#: evidence for "merge this", negative against; blockers are not points.
READINESS_WEIGHTS: dict[str, int] = {
    "ci_green": 30,
    "ci_flaky": 10,
    "approved": 25,
    "committer_approved": 10,
    "operator_approved": 10,
    "clean_review": 15,
    "small_diff": 10,
    "medium_diff": 5,
    "committer_author": 15,
    "known_contributor": 10,
    "new_contributor": -10,
    "tests_good": 10,
    "tests_suspect": -5,
    "touches_tests": 5,
    "fresh": 5,
    "open_nits": -5,
    "learned_dismissals": -20,
}
MAX_READINESS = 100

#: Learned-rule thresholds. Three is the smallest count that is a pattern
#: rather than a mood; the dismissal rate keeps one unlucky author from being
#: penalised for a single "not yet".
LEARNED_MIN_OBSERVATIONS = 3
LEARNED_DISMISS_RATE = 0.6
LEARNED_BLOCKER_MIN = 3

#: Reason codes that become blockers once they recur, and the gap each one
#: names. "needs-committer-review" means "no committer has approved".
_LEARNED_BLOCKER_GAPS: dict[str, str] = {
    "needs-tests": "no test changes",
    "needs-committer-review": "no committer approval",
}

# "suite" because Spark (and most Scala projects) name tests ``FooSuite.scala``,
# which the first two never match — and Spark is the repo this was built for.
_TEST_PATH_HINTS: tuple[str, ...] = ("test", "spec", "suite")


# --------------------------------------------------------------------------
# CI classification (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CICheck:
    """One CI result on a commit, in the shape ``ForgeClient.get_commit_checks`` returns."""

    name: str
    status: str = "completed"
    conclusion: str = ""
    url: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> CICheck | None:
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            return None
        return cls(
            name=name,
            status=str(raw.get("status") or "completed"),
            conclusion=str(raw.get("conclusion") or ""),
            url=str(raw.get("url") or ""),
        )

    @property
    def failed(self) -> bool:
        return self.status == "completed" and self.conclusion in FAILED_CONCLUSIONS

    @property
    def passed(self) -> bool:
        return self.status == "completed" and self.conclusion in PASSED_CONCLUSIONS

    @property
    def running(self) -> bool:
        return self.status != "completed"


@dataclass
class CIAssessment:
    """Green / flaky / red / pending / unknown, and why."""

    status: str = CI_UNKNOWN
    total: int = 0
    failing: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    #: Per failing check: how likely it is a flake, 0-1.
    flake_confidence: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def is_flaky(self) -> bool:
        return self.status == CI_FLAKY

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "total": self.total,
            "failing": list(self.failing),
            "pending": list(self.pending),
            "flake_confidence": {k: round(v, 2) for k, v in self.flake_confidence.items()},
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> CIAssessment:
        if not raw:
            return cls()
        failing = raw.get("failing")
        pending = raw.get("pending")
        conf = raw.get("flake_confidence")
        reasons = raw.get("reasons")
        total = raw.get("total")
        return cls(
            status=str(raw.get("status") or CI_UNKNOWN),
            total=int(total) if isinstance(total, int) else 0,
            failing=[str(x) for x in failing] if isinstance(failing, list) else [],
            pending=[str(x) for x in pending] if isinstance(pending, list) else [],
            flake_confidence=(
                {str(k): float(v) for k, v in conf.items()} if isinstance(conf, dict) else {}
            ),
            reasons=[str(x) for x in reasons] if isinstance(reasons, list) else [],
        )


def parse_checks(raw_checks: Iterable[Mapping[str, object]]) -> list[CICheck]:
    """Turn stored/fetched check dicts into :class:`CICheck`, dropping malformed ones."""
    parsed: list[CICheck] = []
    for raw in raw_checks:
        check = CICheck.from_dict(raw)
        if check is not None:
            parsed.append(check)
    return parsed


def summarize_ci_status(checks: Sequence[CICheck]) -> str:
    """``PullRequest.ci_status``: ``pass`` / ``fail`` / ``pending`` / ``""``.

    The merge queue's eligibility check reads this field; it predates the
    richer assessment and keeps its three-word vocabulary.
    """
    if not checks:
        return ""
    if any(c.failed for c in checks):
        return "fail"
    if any(c.running for c in checks):
        return "pending"
    return "pass"


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch(lowered, p.lower()) for p in patterns if p)


def flake_confidence(
    check: CICheck,
    *,
    flaky_patterns: Iterable[str] = (),
    deterministic_patterns: Iterable[str] = DEFAULT_DETERMINISTIC_CHECKS,
    history: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[float, str]:
    """How likely one failing check is to be a flake, and the one-line reason.

    A name-based prior (configured flaky pattern > timed-out/cancelled >
    unknown > lint-ish deterministic name) blended with the project's learned
    history for that check name, Laplace-smoothed. The history's weight grows
    with how much of it there is: ``n / (n + 2)``, so two observations are an
    even split with the prior and six mostly override it. That is what lets
    the operator's "this failure is real" beat a configured-flaky pattern
    after a few repeats, and lets a check nobody configured earn flaky status
    from rebases that went green.
    """
    if _matches_any(check.name, flaky_patterns):
        prior, why = _CONFIGURED_FLAKY_PRIOR, "configured as flaky"
    elif check.conclusion in _CONCLUSION_FLAKE_PRIOR:
        prior, why = _CONCLUSION_FLAKE_PRIOR[check.conclusion], check.conclusion.replace("_", " ")
    elif _matches_any(check.name, deterministic_patterns):
        prior, why = _DETERMINISTIC_PRIOR, "lint/type/compile checks don't flake"
    else:
        prior, why = _DEFAULT_FLAKE_PRIOR, "no history for this check"

    flaky, real = (history or {}).get(check.name, (0, 0))
    observations = flaky + real
    if observations <= 0:
        return prior, why
    learned = (flaky + 1) / (observations + 2)
    weight = observations / (observations + 2)
    blended = (1 - weight) * prior + weight * learned
    history_why = f"flaked {flaky}x, real {real}x before"
    return round(blended, 3), f"{why}; {history_why}"


def classify_ci(
    raw_checks: Iterable[Mapping[str, object]] | Sequence[CICheck],
    *,
    flaky_patterns: Iterable[str] = (),
    deterministic_patterns: Iterable[str] = DEFAULT_DETERMINISTIC_CHECKS,
    history: Mapping[str, tuple[int, int]] | None = None,
) -> CIAssessment:
    """Green / flaky / red / pending / unknown for one commit's checks.

    Flaky means every failing check clears :data:`FLAKE_THRESHOLD` — one real
    failure makes the whole run red, since the author has to fix that one
    anyway. A run where more than half the checks fail is red regardless:
    that is a broken build or a broken base, not a flake. Pending beats
    green (something is still running) but not red.
    """
    checks: list[CICheck] = []
    for raw in raw_checks:
        parsed = raw if isinstance(raw, CICheck) else CICheck.from_dict(raw)
        if parsed is not None:
            checks.append(parsed)
    assessment = CIAssessment(total=len(checks))
    if not checks:
        assessment.reasons.append("no CI results for this commit")
        return assessment

    failing = [c for c in checks if c.failed]
    pending = [c for c in checks if c.running]
    assessment.failing = [c.name for c in failing]
    assessment.pending = [c.name for c in pending]

    if not failing:
        if pending:
            assessment.status = CI_PENDING
            assessment.reasons.append(f"{len(pending)} check(s) still running")
        else:
            assessment.status = CI_GREEN
            assessment.reasons.append(f"all {len(checks)} check(s) passed")
        return assessment

    for check in failing:
        conf, why = flake_confidence(
            check,
            flaky_patterns=flaky_patterns,
            deterministic_patterns=deterministic_patterns,
            history=history,
        )
        assessment.flake_confidence[check.name] = conf
        assessment.reasons.append(f"{check.name}: {why}")

    majority_failing = len(failing) > max(2, len(checks) // 2)
    all_flaky = all(c >= FLAKE_THRESHOLD for c in assessment.flake_confidence.values())
    if all_flaky and not majority_failing:
        assessment.status = CI_FLAKY
        assessment.reasons.insert(
            0, f"{len(failing)} of {len(checks)} check(s) failed, probably flaky"
        )
    else:
        assessment.status = CI_RED
        if majority_failing:
            assessment.reasons.insert(
                0, f"{len(failing)} of {len(checks)} check(s) failed — not a flake"
            )
        else:
            assessment.reasons.insert(0, f"{len(failing)} of {len(checks)} check(s) failed")
    return assessment


# --------------------------------------------------------------------------
# Readiness (pure)
# --------------------------------------------------------------------------


def size_bucket(additions: int, deletions: int) -> str:
    """small (<100 lines) / medium (<500) / large (<1000) / huge."""
    total = additions + deletions
    if total < 100:
        return "small"
    if total < 500:
        return "medium"
    if total < 1000:
        return "large"
    return "huge"


def _top_level(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "(root)"


def feature_tokens(
    author: str,
    labels: Iterable[str],
    changed_files: Iterable[str],
    additions: int,
    deletions: int,
) -> list[str]:
    """The features a dismissal is remembered by.

    Coarse on purpose: an author, a label, a top-level directory, a size
    bucket. Fine enough to say "PRs touching ``sql/`` from this author keep
    coming back", coarse enough that three dismissals can form a pattern.
    """
    tokens = [f"author:{author.lower()}"] if author else []
    tokens.extend(f"label:{lbl.lower()}" for lbl in labels if lbl)
    tokens.extend(sorted({f"path:{_top_level(f)}" for f in changed_files if f}))
    tokens.append(f"size:{size_bucket(additions, deletions)}")
    return tokens


def touches_tests(changed_files: Iterable[str]) -> bool:
    return any(h in f.lower() for f in changed_files for h in _TEST_PATH_HINTS)


@dataclass
class LearnedAdjustments:
    """What the operator's past verdicts say about PRs like this one.

    ``feature_rates`` maps a feature token to ``(dismissed, total)`` across
    the project's merge feedback; ``reason_counts`` counts dismissal reason
    codes. Built by :func:`learned_adjustments_for`; empty means nothing
    learned yet, which is a valid state with no penalties.
    """

    feature_rates: dict[str, tuple[int, int]] = field(default_factory=dict)
    reason_counts: dict[str, int] = field(default_factory=dict)

    def worst_feature(self, tokens: Iterable[str]) -> tuple[str, int, int] | None:
        """The PR's feature with the highest dismissal rate that clears the bar."""
        worst: tuple[str, int, int] | None = None
        worst_rate = 0.0
        for token in tokens:
            dismissed, total = self.feature_rates.get(token, (0, 0))
            if total < LEARNED_MIN_OBSERVATIONS:
                continue
            rate = dismissed / total
            if rate >= LEARNED_DISMISS_RATE and rate > worst_rate:
                worst, worst_rate = (token, dismissed, total), rate
        return worst

    def blocker_reasons(self) -> list[str]:
        """Reason codes dismissed often enough to block the next PR with that gap."""
        return sorted(
            code
            for code, count in self.reason_counts.items()
            if code in _LEARNED_BLOCKER_GAPS and count >= LEARNED_BLOCKER_MIN
        )

    def summary_lines(self) -> list[str]:
        """The learned rules, one sentence each, for the dashboard."""
        lines: list[str] = []
        for code in self.blocker_reasons():
            gap = _LEARNED_BLOCKER_GAPS[code]
            lines.append(
                f"You've sent back {self.reason_counts[code]} PRs for {code!r}: "
                f"a PR with {gap} no longer counts as almost ready."
            )
        penalised = sorted(
            (token, d, t)
            for token, (d, t) in self.feature_rates.items()
            if t >= LEARNED_MIN_OBSERVATIONS and d / t >= LEARNED_DISMISS_RATE
        )
        for token, dismissed, total in penalised:
            lines.append(
                f"Sent back {dismissed} of {total} candidates matching {token}: "
                f"similar PRs are docked {-READINESS_WEIGHTS['learned_dismissals']} points."
            )
        return lines


@dataclass
class ReadinessInput:
    """Everything :func:`evaluate_merge_readiness` looks at. No ORM objects."""

    author: str = ""
    operator_username: str = ""
    title: str = ""
    is_draft: bool = False
    mergeable: bool | None = None
    additions: int = 0
    deletions: int = 0
    changed_files: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    #: login -> {"state": ..., "commit_id": ...}; see PullRequest.review_states.
    review_states: dict[str, dict[str, str]] = field(default_factory=dict)
    head_sha: str = ""
    committers: list[str] = field(default_factory=list)
    known_contributors: list[str] = field(default_factory=list)
    is_new_contributor: bool = False
    #: Live, unposted findings from frank's own review, by severity.
    open_findings: dict[str, int] = field(default_factory=dict)
    #: Whether any reviewer actually ran on this head (so "no findings" means clean).
    review_ran: bool = False
    test_verdict: str | None = None
    days_since_update: float | None = None
    ci: CIAssessment = field(default_factory=CIAssessment)
    dismissed_sha: str = ""
    learned: LearnedAdjustments = field(default_factory=LearnedAdjustments)


@dataclass
class MergeReadiness:
    """The verdict, and the two lists the operator actually reads."""

    verdict: str = VERDICT_NOT_READY
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    breakdown: dict[str, float] = field(default_factory=dict)
    ci: CIAssessment = field(default_factory=CIAssessment)
    approvers: list[str] = field(default_factory=list)
    stale_approvers: list[str] = field(default_factory=list)
    changes_requested_by: list[str] = field(default_factory=list)

    @property
    def listed(self) -> bool:
        return self.verdict in LISTED_VERDICTS

    def to_detail(self) -> dict[str, object]:
        """The JSON stored on ``PullRequest.merge_readiness_detail``."""
        return {
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "breakdown": dict(self.breakdown),
            "ci": self.ci.to_dict(),
            "approvers": list(self.approvers),
            "stale_approvers": list(self.stale_approvers),
            "changes_requested_by": list(self.changes_requested_by),
            "evaluated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        }


def split_reviews(
    review_states: Mapping[str, Mapping[str, str]], head_sha: str
) -> tuple[list[str], list[str], list[str]]:
    """``(approvers, stale_approvers, changes_requested_by)`` from stored review states.

    An approval on an older commit still counts, at half weight: GitHub
    keeps it unless the repo dismisses stale reviews, and a committer who
    approved before a typo fix did not change their mind.
    """
    approvers: list[str] = []
    stale: list[str] = []
    changes: list[str] = []
    for login, state in sorted(review_states.items()):
        verdict = str(state.get("state", "")).upper()
        if verdict == "APPROVED":
            commit = str(state.get("commit_id", ""))
            if head_sha and commit and commit != head_sha:
                stale.append(login)
            else:
                approvers.append(login)
        elif verdict == "CHANGES_REQUESTED":
            changes.append(login)
    return approvers, stale, changes


def evaluate_merge_readiness(inp: ReadinessInput, config: MergeReadinessConfig) -> MergeReadiness:
    """Score one PR for "could I merge or LGTM this right now?".

    Blockers are hard stops and are listed even when the score is high, so
    the detail page can say exactly what stands between a PR and the queue.
    Points are evidence; the verdict is ``ready`` (or ``flaky-ci`` when the
    only CI problem looks like a flake) at ``config.ready_threshold``.
    """
    result = MergeReadiness(ci=inp.ci)
    breakdown = result.breakdown
    reasons = result.reasons
    blockers = result.blockers
    op = inp.operator_username.lower()
    author = inp.author.lower()

    def add(name: str, note: str | None = None) -> None:
        breakdown[name] = float(READINESS_WEIGHTS[name])
        if note:
            reasons.append(note)

    # --- hard stops ---------------------------------------------------------
    if inp.is_draft or is_wip_title(inp.title):
        blockers.append("draft / WIP")
    if inp.mergeable is False:
        blockers.append("merge conflicts — needs a rebase")
    if inp.dismissed_sha and inp.dismissed_sha == inp.head_sha:
        blockers.append("you marked this not ready at this commit")

    approvers, stale_approvers, changes = split_reviews(inp.review_states, inp.head_sha)
    result.approvers, result.stale_approvers, result.changes_requested_by = (
        approvers,
        stale_approvers,
        changes,
    )
    if changes:
        blockers.append(f"changes requested by {', '.join(changes)}")

    total_lines = inp.additions + inp.deletions
    if total_lines > config.max_diff_lines:
        blockers.append(f"{total_lines} changed lines is too big for a quick merge")

    if (
        config.stale_after_days > 0
        and inp.days_since_update is not None
        and inp.days_since_update > config.stale_after_days
    ):
        blockers.append(f"no activity for {int(inp.days_since_update)} days")

    serious = inp.open_findings.get("critical", 0) + inp.open_findings.get("important", 0)
    if serious:
        blockers.append(f"{serious} unresolved critical/important finding(s) from your review")

    if inp.test_verdict == "broken":
        blockers.append("differential tests fail on the PR branch")

    # --- CI ------------------------------------------------------------------
    ci = inp.ci
    match ci.status:
        case "green":
            add("ci_green", "CI green")
        case "flaky":
            add(
                "ci_flaky",
                "CI failed on "
                + ", ".join(ci.failing)
                + " — probably a flake (a rebase should clear it)",
            )
        case "red":
            blockers.append("CI failing: " + ", ".join(ci.failing))
        case "pending":
            blockers.append("CI still running")
        case _:
            if config.require_ci:
                blockers.append("no CI results for this commit")

    # --- reviews -------------------------------------------------------------
    committers = {c.lower() for c in inp.committers}
    committer_approvers = [a for a in approvers if a.lower() in committers]
    if approvers:
        add("approved", f"approved by {', '.join(approvers)}")
    elif stale_approvers:
        breakdown["approved"] = READINESS_WEIGHTS["approved"] / 2
        reasons.append(
            f"approved by {', '.join(stale_approvers)} before the latest push (half credit)"
        )
    if committer_approvers:
        add("committer_approved", f"committer approval from {', '.join(committer_approvers)}")
    elif config.require_committer_approval:
        blockers.append("no committer approval yet")
    if op and op in {a.lower() for a in approvers}:
        add("operator_approved", "you already approved this on the forge")

    # --- frank's own review ---------------------------------------------------
    if inp.review_ran and not serious:
        add("clean_review", "your review found nothing serious")
    nits = inp.open_findings.get("nit", 0)
    if nits:
        add("open_nits", f"{nits} open nit(s) — fine to merge with or without")

    # --- size, author, tests, freshness -----------------------------------------
    bucket = size_bucket(inp.additions, inp.deletions)
    if bucket == "small":
        add("small_diff", f"small change ({total_lines} lines)")
    elif bucket == "medium":
        add("medium_diff", f"medium change ({total_lines} lines)")

    known = {c.lower() for c in inp.known_contributors}
    if author and author in committers:
        add("committer_author", f"{inp.author} is a committer")
    elif author and (author in known or author == op):
        add("known_contributor", f"{inp.author} is a known contributor")
    elif inp.is_new_contributor:
        add("new_contributor", f"{inp.author} is a new contributor — worth a closer look")

    if inp.test_verdict == "good":
        add("tests_good", "new tests fail without the change (differential run)")
    elif inp.test_verdict == "suspect":
        add("tests_suspect", "differential tests pass with and without the change")

    if touches_tests(inp.changed_files):
        add("touches_tests", "touches tests")

    if inp.days_since_update is not None and inp.days_since_update <= 14:
        add("fresh", "active in the last two weeks")

    # --- learned from what you said before ----------------------------------------
    tokens = feature_tokens(inp.author, inp.labels, inp.changed_files, inp.additions, inp.deletions)
    worst = inp.learned.worst_feature(tokens)
    if worst is not None:
        token, dismissed, total = worst
        add(
            "learned_dismissals",
            f"you sent back {dismissed} of {total} candidates like this ({token})",
        )
    learned_blockers = inp.learned.blocker_reasons()
    if "needs-tests" in learned_blockers and not touches_tests(inp.changed_files):
        blockers.append(
            f"no test changes (you've sent back {inp.learned.reason_counts['needs-tests']} "
            "candidates for that)"
        )
    if "needs-committer-review" in learned_blockers and not committer_approvers:
        blockers.append(
            "no committer approval (you've sent back "
            f"{inp.learned.reason_counts['needs-committer-review']} candidates for that)"
        )

    raw = sum(breakdown.values())
    result.score = round(max(0.0, min(1.0, raw / MAX_READINESS)), 4)
    if blockers:
        result.verdict = VERDICT_NOT_READY
    elif result.score >= config.ready_threshold:
        result.verdict = VERDICT_FLAKY_CI if ci.is_flaky else VERDICT_READY
    else:
        result.verdict = VERDICT_NOT_READY
        blockers.append(
            f"readiness {round(result.score * 100)} below the {round(config.ready_threshold * 100)} bar"
        )
    return result


# --------------------------------------------------------------------------
# Forge → PullRequest row
# --------------------------------------------------------------------------


def _is_bot(login: str) -> bool:
    return login.lower().endswith("[bot]")


def normalize_reviews(
    raw_reviews: Iterable[Mapping[str, Any]], *, author: str
) -> dict[str, dict[str, str]]:
    """Latest meaningful review per reviewer, in ``PullRequest.review_states`` shape.

    Reviews arrive oldest first. ``COMMENTED`` and ``PENDING`` do not change
    a reviewer's standing; ``DISMISSED`` removes it; ``APPROVED`` and
    ``CHANGES_REQUESTED`` replace it. The author reviewing their own PR and
    ``[bot]`` accounts are ignored — an app's "approval" is not a person's.
    Gitea's ``REQUEST_CHANGES`` / ``COMMENT`` spellings are folded in.
    """
    states: dict[str, dict[str, str]] = {}
    author_l = author.lower()
    for review in raw_reviews:
        user = review.get("user") or {}
        login = str(user.get("login") or "") if isinstance(user, Mapping) else ""
        if not login or login.lower() == author_l or _is_bot(login):
            continue
        state = str(review.get("state") or "").upper()
        if state == "REQUEST_CHANGES":
            state = "CHANGES_REQUESTED"
        if state == "DISMISSED":
            states.pop(login, None)
            continue
        if state not in ("APPROVED", "CHANGES_REQUESTED"):
            continue
        states[login] = {
            "state": state,
            "commit_id": str(review.get("commit_id") or ""),
            "submitted_at": str(review.get("submitted_at") or ""),
        }
    return states


#: Forges whose client raised NotImplementedError for the review/CI calls, so
#: the "this forge has no review data" line is logged once per process per
#: forge class rather than once per PR per cycle.
_warned_unsupported: set[str] = set()


def refresh_review_and_ci_state(
    client: ForgeClient, pr: PullRequest, project_config: ProjectConfig
) -> None:
    """Pull reviews and checks for ``pr``'s head onto the row (not saved).

    Three read calls. Each failure is caught on its own and keeps the
    previous value: a rate-limit window must not turn every approval into
    zero. A forge that does not implement the calls is logged once at INFO
    with the setting that turns the queue off, per CLAUDE.md's rule that a
    gate declining to run says so somewhere visible.
    """
    owner, repo = pr.project.owner, pr.project.repo
    forge_name = type(client).__name__
    try:
        raw_reviews = client.list_pull_request_reviews(owner, repo, pr.number)
        pr.review_states = normalize_reviews(raw_reviews, author=pr.author)
        pr.approval_count = sum(
            1 for s in pr.review_states.values() if s.get("state") == "APPROVED"
        )
    except NotImplementedError:
        if forge_name not in _warned_unsupported:
            _warned_unsupported.add(forge_name)
            logger.info(
                "%s has no review/CI data; the almost-ready queue for %s/%s runs on size and "
                "age alone (merge_readiness.enabled: false silences this)",
                forge_name,
                owner,
                repo,
            )
        return
    except Exception:
        logger.warning(
            "Could not fetch reviews for %s/%s#%d; keeping the last known state",
            owner,
            repo,
            pr.number,
        )

    if not pr.head_sha:
        return
    try:
        raw_checks = client.get_commit_checks(owner, repo, pr.head_sha)
    except NotImplementedError:
        return
    except Exception:
        logger.warning(
            "Could not fetch CI checks for %s/%s#%d; keeping the last known state",
            owner,
            repo,
            pr.number,
        )
        return
    checks = parse_checks(raw_checks)
    pr.ci_checks = [
        {"name": c.name, "status": c.status, "conclusion": c.conclusion, "url": c.url}
        for c in checks
    ]
    pr.ci_checks_sha = pr.head_sha
    pr.ci_status = summarize_ci_status(checks)
    note_rebase_outcome(pr, checks)


def note_rebase_outcome(pr: PullRequest, checks: Sequence[CICheck]) -> None:
    """Learn from a rebase we asked for, once the author has pushed.

    For each check that was failing when we asked: green on the new head is
    a confirmed flake, red again is a real failure, still running is "wait".
    Cleared only once every asked-about check has answered, so a partial
    re-run does not forget the question. Writes ``CICheckHistory``; the next
    :func:`classify_ci` on this project reads it back.
    """
    if not pr.rebase_requested_sha or pr.rebase_requested_sha == pr.head_sha:
        return
    if pr.ci_checks_sha != pr.head_sha:
        return
    from franktheunicorn.core.models import CICheckHistory

    by_name = {c.name: c for c in checks}
    asked = [str(n) for n in (pr.rebase_requested_checks or [])]
    unanswered: list[str] = []
    flaky_now: list[str] = []
    real_now: list[str] = []
    for name in asked:
        check = by_name.get(name)
        if check is None:
            # Renamed or removed workflow: nothing to learn from, nothing to wait for.
            continue
        if check.running:
            unanswered.append(name)
        elif check.passed:
            flaky_now.append(name)
        elif check.failed:
            real_now.append(name)
    if unanswered:
        return
    now = datetime.now(tz=UTC)
    for name in flaky_now:
        row, _ = CICheckHistory.objects.get_or_create(project=pr.project, check_name=name)
        row.flaky_count += 1
        row.save(update_fields=["flaky_count", "updated_at"])
    for name in real_now:
        row, _ = CICheckHistory.objects.get_or_create(project=pr.project, check_name=name)
        row.real_count += 1
        row.last_failed_at = now
        row.save(update_fields=["real_count", "last_failed_at", "updated_at"])
    if flaky_now or real_now:
        logger.info(
            "PR #%d rebased after a flake request: %s went green (flaky), %s still failing (real)",
            pr.number,
            ", ".join(flaky_now) or "nothing",
            ", ".join(real_now) or "nothing",
        )
    pr.rebase_requested_sha = ""
    pr.rebase_requested_at = None
    pr.rebase_requested_checks = []


# --------------------------------------------------------------------------
# Evaluate and store
# --------------------------------------------------------------------------


#: The ``PullRequest`` columns :func:`refresh_merge_readiness` and
#: :func:`refresh_review_and_ci_state` write, for callers' ``update_fields``.
READINESS_UPDATE_FIELDS: tuple[str, ...] = (
    "review_states",
    "approval_count",
    "ci_checks",
    "ci_checks_sha",
    "ci_status",
    "merge_readiness_score",
    "merge_readiness_verdict",
    "merge_readiness_detail",
    "rebase_requested_sha",
    "rebase_requested_at",
    "rebase_requested_checks",
)


def check_history_for(project: Project) -> dict[str, tuple[int, int]]:
    """``check_name -> (flaky_count, real_count)`` for :func:`classify_ci`."""
    from franktheunicorn.core.models import CICheckHistory

    return {
        name: (int(flaky), int(real))
        for name, flaky, real in CICheckHistory.objects.filter(project=project).values_list(
            "check_name", "flaky_count", "real_count"
        )
    }


def learned_adjustments_for(project: Project) -> LearnedAdjustments:
    """Aggregate the project's merge feedback into :class:`LearnedAdjustments`.

    LGTM and merged are positive observations, not-ready is a dismissal,
    rebase-requested is neither (it is a question, not a verdict).
    """
    from franktheunicorn.core.models import MergeReadinessFeedback

    learned = LearnedAdjustments()
    rows = MergeReadinessFeedback.objects.filter(
        project=project, verdict__in=("lgtm", "merged", "not-ready")
    ).values_list("verdict", "reason_code", "features")
    for verdict, reason_code, features in rows:
        dismissed = verdict == "not-ready"
        if dismissed and reason_code:
            learned.reason_counts[reason_code] = learned.reason_counts.get(reason_code, 0) + 1
        tokens = features.get("tokens") if isinstance(features, dict) else None
        if not isinstance(tokens, list):
            continue
        for token in tokens:
            d, t = learned.feature_rates.get(str(token), (0, 0))
            learned.feature_rates[str(token)] = (d + (1 if dismissed else 0), t + 1)
    return learned


def _open_findings(pr: PullRequest) -> tuple[dict[str, int], bool]:
    """Live unposted findings by severity, and whether any reviewer ran on this head."""
    from django.db.models import Count

    from franktheunicorn.core.models import ReviewDraft

    counts = dict(
        ReviewDraft.objects.filter(
            pull_request=pr,
            line_number__isnull=False,
            is_auto_suppressed=False,
            status__in=("pending", "accepted", "edited"),
        )
        .values_list("severity")
        .annotate(c=Count("id"))
        .values_list("severity", "c")
    )
    runs = pr.agent_runs if isinstance(pr.agent_runs, dict) else {}
    review_ran = any(
        isinstance(info, dict) and info.get("status") == "ok" and not info.get("stale")
        for info in runs.values()
    )
    return {str(k): int(v) for k, v in counts.items()}, review_ran


def _latest_test_verdict(pr: PullRequest) -> str | None:
    from franktheunicorn.core.models import TestRun

    verdict = (
        TestRun.objects.filter(
            pull_request=pr, status="completed", differential_verdict__isnull=False
        )
        .order_by("-created_at")
        .values_list("differential_verdict", flat=True)
        .first()
    )
    return str(verdict) if verdict else None


def build_readiness_input(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_username: str,
    *,
    learned: LearnedAdjustments | None = None,
    history: Mapping[str, tuple[int, int]] | None = None,
) -> ReadinessInput:
    """Everything the pure evaluator needs, read off the row and its neighbours."""
    cfg = project_config.merge_readiness
    if history is None:
        history = check_history_for(pr.project)
    ci = classify_ci(
        pr.ci_checks or [],
        flaky_patterns=cfg.flaky_checks,
        deterministic_patterns=cfg.deterministic_checks,
        history=history,
    )
    open_findings, review_ran = _open_findings(pr)
    days: float | None = None
    if pr.github_updated_at is not None:
        days = (datetime.now(tz=UTC) - pr.github_updated_at).total_seconds() / 86400
    known = list(pr.project.contributors_cache or []) + list(project_config.frequent_contributors)
    review_states = pr.review_states if isinstance(pr.review_states, dict) else {}
    return ReadinessInput(
        author=pr.author,
        operator_username=operator_username,
        title=pr.title,
        is_draft=pr.is_draft,
        mergeable=pr.mergeable,
        additions=pr.additions,
        deletions=pr.deletions,
        changed_files=[str(f) for f in (pr.changed_files or [])],
        labels=[str(lbl) for lbl in (pr.labels or [])],
        review_states={str(k): dict(v) for k, v in review_states.items() if isinstance(v, dict)},
        head_sha=pr.head_sha,
        committers=list(project_config.committers),
        known_contributors=known,
        is_new_contributor=pr.is_new_contributor,
        open_findings=open_findings,
        review_ran=review_ran,
        test_verdict=_latest_test_verdict(pr),
        days_since_update=days,
        ci=ci,
        dismissed_sha=pr.merge_readiness_dismissed_sha,
        learned=learned if learned is not None else learned_adjustments_for(pr.project),
    )


def refresh_merge_readiness(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_username: str,
    *,
    save: bool = True,
    learned: LearnedAdjustments | None = None,
    history: Mapping[str, tuple[int, int]] | None = None,
) -> MergeReadiness:
    """Evaluate ``pr`` and write the verdict onto the row.

    ``save=False`` for callers about to save the row themselves (the poller
    adds :data:`READINESS_UPDATE_FIELDS` to its own ``update_fields``).
    """
    inp = build_readiness_input(
        pr, project_config, operator_username, learned=learned, history=history
    )
    readiness = evaluate_merge_readiness(inp, project_config.merge_readiness)
    previous = pr.merge_readiness_verdict
    pr.merge_readiness_score = readiness.score
    pr.merge_readiness_verdict = readiness.verdict
    pr.merge_readiness_detail = readiness.to_detail()
    if save:
        pr.save(
            update_fields=[
                "merge_readiness_score",
                "merge_readiness_verdict",
                "merge_readiness_detail",
                "updated_at",
            ]
        )
    if readiness.verdict != previous and readiness.listed:
        logger.info(
            "PR #%d (%s) is almost ready to merge: %s (readiness %d)",
            pr.number,
            pr.project.full_name,
            readiness.verdict,
            round(readiness.score * 100),
        )
    return readiness


# --------------------------------------------------------------------------
# Operator feedback
# --------------------------------------------------------------------------

_VERDICT_TO_ACTION: dict[str, str] = {
    "lgtm": "lgtm_pr",
    "merged": "merge_pr",
    "not-ready": "dismiss_merge_candidate",
    "rebase-requested": "request_rebase",
}


def record_merge_feedback(
    pr: PullRequest,
    verdict: str,
    *,
    reason_code: str = "",
    reason_text: str = "",
    operator_username: str = "",
) -> MergeReadinessFeedback:
    """Write down what the operator said about a candidate, and act on it.

    - ``not-ready`` parks the PR at this head (``merge_readiness_dismissed_sha``)
      and, for ``not-a-flake``, counts a real failure against each failing
      check so the classifier stops calling them flaky.
    - ``lgtm`` records the operator's own approval on the row so the queue
      shows it before the next poll.
    - ``rebase-requested`` remembers which checks were failing, for
      :func:`note_rebase_outcome`.
    - ``merged`` is just the record.

    Every verdict also writes an ``OperatorAction`` for the audit trail. The
    caller re-evaluates readiness afterwards; this function does not.
    """
    from franktheunicorn.core.models import CICheckHistory, MergeReadinessFeedback, OperatorAction

    detail = pr.merge_readiness_detail if isinstance(pr.merge_readiness_detail, dict) else {}
    ci = CIAssessment.from_dict(detail.get("ci") if isinstance(detail.get("ci"), dict) else None)
    approvers_raw = detail.get("approvers")
    approvers = [str(a) for a in approvers_raw] if isinstance(approvers_raw, list) else []
    features: dict[str, object] = {
        "tokens": feature_tokens(
            pr.author,
            [str(lbl) for lbl in (pr.labels or [])],
            [str(f) for f in (pr.changed_files or [])],
            pr.additions,
            pr.deletions,
        ),
        "failing_checks": list(ci.failing),
        "ci_status": ci.status,
        "approvers": approvers,
        "readiness_score": pr.merge_readiness_score,
        "is_new_contributor": pr.is_new_contributor,
    }
    feedback = MergeReadinessFeedback.objects.create(
        pull_request=pr,
        project=pr.project,
        verdict=verdict,
        reason_code=reason_code,
        reason_text=reason_text,
        head_sha=pr.head_sha,
        features=features,
    )
    OperatorAction.objects.create(
        action_type=_VERDICT_TO_ACTION.get(verdict, "dismiss_merge_candidate"),
        pull_request=pr,
        notes=f"{reason_code}: {reason_text}".strip(": ") if (reason_code or reason_text) else "",
    )

    now = datetime.now(tz=UTC)
    update_fields: list[str] = []
    if verdict == "not-ready":
        pr.merge_readiness_dismissed_sha = pr.head_sha
        update_fields.append("merge_readiness_dismissed_sha")
        if reason_code == "not-a-flake":
            for name in ci.failing:
                row, _ = CICheckHistory.objects.get_or_create(project=pr.project, check_name=name)
                row.real_count += 1
                row.last_failed_at = now
                row.save(update_fields=["real_count", "last_failed_at", "updated_at"])
            if ci.failing:
                logger.info(
                    "PR #%d: operator says the failure on %s is real; noted against the check(s)",
                    pr.number,
                    ", ".join(ci.failing),
                )
    elif verdict == "lgtm" and operator_username:
        states = dict(pr.review_states) if isinstance(pr.review_states, dict) else {}
        states[operator_username] = {
            "state": "APPROVED",
            "commit_id": pr.head_sha,
            "submitted_at": now.isoformat(timespec="seconds"),
        }
        pr.review_states = states
        pr.approval_count = sum(1 for s in states.values() if s.get("state") == "APPROVED")
        update_fields.extend(["review_states", "approval_count"])
    elif verdict == "rebase-requested":
        pr.rebase_requested_sha = pr.head_sha
        pr.rebase_requested_at = now
        pr.rebase_requested_checks = list(ci.failing)
        update_fields.extend(
            ["rebase_requested_sha", "rebase_requested_at", "rebase_requested_checks"]
        )
    if update_fields:
        pr.save(update_fields=[*update_fields, "updated_at"])
    return feedback


def build_rebase_request(pr: PullRequest, config: MergeReadinessConfig) -> str:
    """The comment the "Ask to rebase" button posts, from the project's template."""
    detail = pr.merge_readiness_detail if isinstance(pr.merge_readiness_detail, dict) else {}
    ci = CIAssessment.from_dict(detail.get("ci") if isinstance(detail.get("ci"), dict) else None)
    checks = ", ".join(f"`{name}`" for name in ci.failing) or "CI"
    base_branch = pr.base_branch or "main"
    try:
        return config.rebase_request_template.format(
            author=pr.author, checks=checks, base_branch=base_branch
        )
    except (KeyError, IndexError, ValueError):
        # A template with a typo'd placeholder should degrade to a usable
        # comment, not a 500 on the button.
        logger.warning(
            "merge_readiness.rebase_request_template has an unknown placeholder; using the default"
        )
        from franktheunicorn.config.models import DEFAULT_REBASE_REQUEST_TEMPLATE

        return DEFAULT_REBASE_REQUEST_TEMPLATE.format(
            author=pr.author, checks=checks, base_branch=base_branch
        )


def learned_rules_summary(project: Project) -> list[str]:
    """What the queue has learned for ``project``, for the tab header.

    Feedback-derived rules first, then the checks the project now treats as
    flaky (or has stopped treating as flaky) from rebase outcomes.
    """
    from franktheunicorn.core.models import CICheckHistory

    lines = learned_adjustments_for(project).summary_lines()
    for row in CICheckHistory.objects.filter(project=project).order_by("check_name"):
        total = row.flaky_count + row.real_count
        if total <= 0:
            continue
        conf, _ = flake_confidence(
            CICheck(name=row.check_name, conclusion="failure"),
            history={row.check_name: (row.flaky_count, row.real_count)},
        )
        verdict = "probably flaky" if conf >= FLAKE_THRESHOLD else "treated as real"
        lines.append(
            f"{row.check_name}: {verdict} ({row.flaky_count} flake(s), "
            f"{row.real_count} real failure(s) observed)."
        )
    return lines
