"""Intending-to-merge detection.

Answers one question per PR: has the operator (or another configured
approver) already said this is good to go? Two shapes of "yes":

- **approved** — a formal APPROVED review, or an unconditional "LGTM" /
  "looks good to me" / "+1" from an approver. The follow-up is to merge it.
- **conditional** — "looks good pending CI", "will merge once the tests pass",
  "LGTM assuming the build is green". The follow-up is to check CI and then
  merge it.

Either way the PR is no longer waiting on the operator's *review*; it is
waiting on the operator's *merge*, and those are the ones that get forgotten:
the review is done, the tab is closed, and the PR sits approved for a week
until the author pings. The ``intending-to-merge`` queue is that follow-up
list, oldest approval first.

Detection is a pure function over the PR's reviews and conversation comments
(GitHub-shaped dicts, which is what every forge client normalises to), so it
runs on whatever the poller already fetched. Signals are read in time order
and the approver's latest one wins: an approval followed by CHANGES_REQUESTED,
by a dismissal of that review, or by the approver taking it back in a comment
("-1", "retracting my LGTM", "please hold off merging") clears the intent; an
approval followed by an unrelated nit does not, because "LGTM, one typo" is
still an approval. The withdrawal rules follow Apache Magpie's vote-tally,
where one person's latest vote is the one that counts, a "-1" is a no and a
retraction is not a vote at all (``plugins/magpie-release-management/skills/
vote-tally/SKILL.md``, Step 1 and "One person, one vote", apache/magpie commit
dadb315caa8b881fc1d2195bef03223dac25915a, Apache-2.0).

The forge's own idea of approval (``approval_count``) is computed alongside,
across *all* reviewers, because the merge queue's eligibility check reads it
and nothing wrote it before this.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from franktheunicorn.core.models import PullRequest

#: ``PullRequest.merge_intent`` values.
INTENT_NONE = ""
INTENT_APPROVED = "approved"
INTENT_CONDITIONAL = "conditional"
#: What :func:`classify_body` returns for an approver taking an approval back.
#: Never stored: it clears the intent, like CHANGES_REQUESTED.
INTENT_WITHDRAWN = "withdrawn"

#: How much of the matched comment the dashboard keeps.
EXCERPT_MAX_CHARS = 300

# A conditional approval has a "yes" and a "but": a positive verdict and a
# gate it is waiting on. Matching both halves, in either order, is what keeps
# "LGTM" + "CI is red, please fix" (a plain comment about CI) from counting.
_VERDICT = (
    r"(?P<verdict>lgtm|looks\s+good(?:\s+to\s+me)?|good\s+to\s+go|ship\s+it|"
    r"\+1|(?:i(?:'ll|\s+will|\s+am\s+going\s+to|'m\s+going\s+to)?\s+)?"
    r"(?:merge|merging|land|landing))"
)
_GATE_WORD = r"(?:pending|once|when|after|if|assuming|provided|as\s+long\s+as|waiting\s+(?:on|for))"
_GATE_SUBJECT = (
    r"(?:ci|tests?|checks?|build|builds|jenkins|github\s+actions|actions|"
    r"pipeline|green|passes|pass|passing)"
)

_CONDITIONAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "LGTM pending CI", "merging once tests pass", "looks good if the build is green"
    re.compile(
        rf"(?<!\w){_VERDICT}\b[^.\n]{{0,80}}?\b{_GATE_WORD}\b[^.\n]{{0,60}}?\b{_GATE_SUBJECT}\b"
    ),
    # "pending CI, LGTM", "once the tests pass this looks good"
    re.compile(
        rf"\b{_GATE_WORD}\b[^.\n]{{0,60}}?\b{_GATE_SUBJECT}\b[^.\n]{{0,80}}?(?<!\w){_VERDICT}\b"
    ),
)

# An unconditional approval said in a comment rather than with the button.
# Anchored to the start of a line: "LGTM" leading a comment is a verdict,
# "not sure this LGTM" in the middle of a paragraph is not.
_PLAIN_APPROVAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:>.*\n\s*)*(?:lgtm|looks\s+good(?:\s+to\s+me)?|ship\s+it|good\s+to\s+go)\b"),
    re.compile(r"^\s*(?:>.*\n\s*)*\+1\b"),
    re.compile(
        r"^\s*(?:>.*\n\s*)*(?:i(?:'ll|\s+will|\s+am\s+going\s+to|'m\s+going\s+to)\s+merge)\b"
    ),
)

# An approver taking it back. Checked after the approval patterns, so "LGTM,
# but don't merge until the release is cut" stays an approval. A retraction
# has to be the speaker's own ("my LGTM"): a review discussing a feature that
# revokes approvals is not one. Each carries a ``verdict`` group for
# ``_is_affirmative``, so "should I retract my +1?" and "I won't hold off
# merging" do not count.
_WITHDRAWAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "-1, this breaks the shuffle path", leading the comment like "+1" does.
    re.compile(r"^\s*(?:>.*\n\s*)*(?P<verdict>-1)(?![\d.])"),
    # "retracting my LGTM", "I take back my +1", "withdrawing my approval".
    re.compile(
        r"(?P<verdict>\b(?:retract|withdraw|revok|rescind|take\s+back|taking\s+back)\w*)"
        r"\s+(?:\w+\s+){0,2}?my\s+(?:\w+\s+){0,2}?(?:lgtm|\+1|approval)"
    ),
    # "Please hold off merging", "Do not merge yet", "let's hold off on
    # merging": imperative, so sentence-initial or after a softener. "If we
    # don't merge this before the freeze" is not a hold.
    re.compile(
        r"(?:^|[.!?\n,;:]\s*|\b(?:please|pls|let'?s|let\s+us|we\s+should|i'd)\s+)"
        r"(?P<verdict>hold\s+off(?:\s+on)?\s+merg\w*|do\s+not\s+merge\b|don'?t\s+merge\b)"
    ),
    # "This is not ready to merge", "it's not ready to be merged": about this
    # PR, not "the other PR is not ready to merge".
    re.compile(
        r"(?P<verdict>\b(?:this(?:\s+pr)?|it)(?:\s+is|'s)?\s+not\s+(?:yet\s+)?ready\s+to\s+"
        r"(?:be\s+)?merged?\b)"
    ),
)

# "Please do not merge once CI passes" and "I cannot merge after the tests
# pass" have a verdict and a gate and mean the opposite. A negation in the
# few words before the verdict, inside the same sentence, flips it.
_NEGATION_BEFORE = re.compile(
    r"(?:\bnot|n't|\bnever|\bcannot|\bwithout|\bdon't|\bwon't)\s+(?:\w+\s+){0,2}$"
)
_SENTENCE_END = re.compile(r"[.!?\n]")

# Lines quoting someone else ("> LGTM pending CI") are not the approver's
# words. Fenced code is not either.
_QUOTE_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)


@dataclass
class MergeIntent:
    """What the approver last said about merging this PR."""

    kind: str = INTENT_NONE
    at: datetime | None = None
    #: ``review`` or ``comment``.
    source: str = ""
    by: str = ""
    excerpt: str = ""
    #: The commit the approval was against when the forge says (review
    #: ``commit_id``); empty for a conversation comment.
    commit_sha: str = ""

    @property
    def is_set(self) -> bool:
        return self.kind != INTENT_NONE


@dataclass
class _Signal:
    at: datetime | None
    order: int
    by: str
    source: str
    state: str
    body: str
    commit_sha: str
    extra: dict[str, Any] = field(default_factory=dict)


def _login(obj: dict[str, Any]) -> str:
    user = obj.get("user")
    if isinstance(user, dict):
        return str(user.get("login", "") or "")
    return ""


def _parse_when(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naive means the forge omitted the offset; every forge here reports UTC,
    # and a naive value would make the sort below raise against aware ones.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _strip_quotes(body: str) -> str:
    return _QUOTE_LINE.sub("", _FENCED_CODE.sub("", body))


def _excerpt(body: str, match: re.Match[str] | None) -> str:
    """The sentence the match sits in, for the dashboard, bounded."""
    text = " ".join(_strip_quotes(body).split())
    if not text:
        return ""
    if match is not None:
        # Re-find the match in the whitespace-collapsed text so the excerpt
        # starts at the sentence that carries it rather than the comment's
        # first line, which on a long review is usually "Thanks for the PR!".
        # Lower-cased, since that is what the pattern was matched against.
        collapsed_match = match.re.search(text.lower())
        if collapsed_match is not None:
            start = text.rfind(". ", 0, min(collapsed_match.start(), len(text)))
            text = text[start + 2 :] if start >= 0 else text
    if len(text) > EXCERPT_MAX_CHARS:
        text = text[: EXCERPT_MAX_CHARS - 1].rstrip() + "…"
    return text


def classify_body(body: str) -> tuple[str, re.Match[str] | None]:
    """Classify free text as ``conditional``, ``approved``, ``withdrawn`` or
    nothing.

    Conditional is checked first: "LGTM pending CI" also matches the plain
    "LGTM" pattern, and the gated reading is the one that changes what the
    operator does next. Withdrawal is checked last, so a comment that both
    approves and says "don't merge yet" keeps the approval it would have had.
    """
    text = _strip_quotes(body or "")
    lowered = text.lower()
    for pattern in _CONDITIONAL_PATTERNS:
        for match in pattern.finditer(lowered):
            if _is_affirmative(lowered, match):
                return INTENT_CONDITIONAL, match
    for pattern in _PLAIN_APPROVAL_PATTERNS:
        plain = pattern.search(lowered)
        if plain is not None:
            return INTENT_APPROVED, plain
    for pattern in _WITHDRAWAL_PATTERNS:
        for match in pattern.finditer(lowered):
            if _is_affirmative(lowered, match):
                return INTENT_WITHDRAWN, match
    return INTENT_NONE, None


def _is_affirmative(lowered: str, match: re.Match[str]) -> bool:
    """False for a negated verdict or a question.

    "Can you merge when the build is green?" asks; "please do not merge once
    CI passes" forbids. Both carry the verdict/gate shape and neither is an
    approval. The sentence is the unit: a "not" in the previous sentence, or
    after the verdict ("LGTM, not blocking"), does not count.
    """
    verdict_start = match.start("verdict")
    sentence_start = 0
    for boundary in _SENTENCE_END.finditer(lowered, 0, verdict_start):
        sentence_start = boundary.end()
    end_match = _SENTENCE_END.search(lowered, match.end())
    sentence_end = end_match.start() if end_match else len(lowered)
    if end_match is not None and end_match.group() == "?":
        return False
    if lowered[sentence_start:sentence_end].rstrip().endswith("?"):
        return False
    return _NEGATION_BEFORE.search(lowered[sentence_start:verdict_start]) is None


def _signals(
    reviews: Iterable[dict[str, Any]],
    comments: Iterable[dict[str, Any]],
) -> list[_Signal]:
    found: list[_Signal] = []
    order = 0
    for review in reviews:
        state = str(review.get("state", "") or "").upper()
        if state == "PENDING":
            # An unsubmitted draft review: nobody but its author can see it,
            # and it says nothing yet.
            continue
        found.append(
            _Signal(
                at=_parse_when(review.get("submitted_at")),
                order=order,
                by=_login(review),
                source="review",
                state=state,
                body=str(review.get("body", "") or ""),
                commit_sha=str(review.get("commit_id", "") or ""),
            )
        )
        order += 1
    for comment in comments:
        found.append(
            _Signal(
                at=_parse_when(comment.get("created_at")),
                order=order,
                by=_login(comment),
                source="comment",
                state="",
                body=str(comment.get("body", "") or ""),
                commit_sha="",
            )
        )
        order += 1
    # Time order, with undated items (a GitLab approval has no timestamp)
    # first so anything dated can override them. ``order`` keeps the sort
    # stable for equal timestamps.
    found.sort(key=lambda s: (s.at is not None, s.at or datetime.min, s.order))
    return found


def detect_merge_intent(
    reviews: Iterable[dict[str, Any]],
    comments: Iterable[dict[str, Any]],
    approvers: Sequence[str],
) -> MergeIntent:
    """The approvers' latest word on merging, read from reviews and comments.

    ``approvers`` are forge logins, compared case-insensitively. The latest
    signal from any of them decides: APPROVED (or an approving comment) sets
    the intent; CHANGES_REQUESTED, a DISMISSED review or a withdrawing comment
    clears it; anything else leaves the previous answer standing. A dismissed
    review is one the forge no longer counts, by a maintainer or by a "dismiss
    stale approvals" rule on push, so its "LGTM" body must not count either.
    """
    wanted = {a.lower() for a in approvers if a}
    if not wanted:
        return MergeIntent()

    intent = MergeIntent()
    for signal in _signals(reviews, comments):
        if signal.by.lower() not in wanted:
            continue
        kind, match = classify_body(signal.body)
        if signal.state in ("CHANGES_REQUESTED", "DISMISSED"):
            intent = MergeIntent()
            continue
        if signal.state == "APPROVED":
            # The button is the verdict; the body only decides whether it was
            # a gated one.
            kind = INTENT_CONDITIONAL if kind == INTENT_CONDITIONAL else INTENT_APPROVED
        if kind == INTENT_WITHDRAWN:
            intent = MergeIntent()
            continue
        if kind == INTENT_NONE:
            continue
        intent = MergeIntent(
            kind=kind,
            at=signal.at,
            source=signal.source,
            by=signal.by,
            excerpt=_excerpt(signal.body, match),
            commit_sha=signal.commit_sha,
        )
    return intent


def count_approvals(reviews: Iterable[dict[str, Any]], *, exclude: str = "") -> int:
    """Reviewers whose *latest* verdict is APPROVED, the way the forge counts.

    A reviewer who approved and then requested changes is not an approval; one
    who approved and then left a plain comment still is. ``exclude`` drops the
    PR author, whose self-review a forge would not count.
    """
    latest: dict[str, str] = {}
    for signal in _signals(reviews, ()):
        if signal.state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        if not signal.by or signal.by.lower() == exclude.lower():
            continue
        latest[signal.by.lower()] = signal.state
    return sum(1 for state in latest.values() if state == "APPROVED")


def apply_merge_intent(pr: PullRequest, intent: MergeIntent) -> list[str]:
    """Write *intent* onto *pr* (unsaved) and return the fields that changed.

    ``merge_intent_commit_sha`` is the head the approval was given against:
    the review's own ``commit_id`` when the forge records one, else the PR
    head at the time the approval was first seen. It is only (re)written when
    the approval itself is new, so a later push shows up as "moved since
    approval" instead of silently re-baselining on every poll.
    """
    changed: list[str] = []
    is_new_approval = intent.is_set and (
        pr.merge_intent == INTENT_NONE or pr.merge_intent_at != intent.at
    )

    def _set(name: str, value: object) -> None:
        if getattr(pr, name) != value:
            setattr(pr, name, value)
            changed.append(name)

    _set("merge_intent", intent.kind)
    _set("merge_intent_at", intent.at)
    _set("merge_intent_source", intent.source)
    _set("merge_intent_by", intent.by)
    _set("merge_intent_excerpt", intent.excerpt)
    if not intent.is_set:
        _set("merge_intent_commit_sha", "")
    elif is_new_approval or not pr.merge_intent_commit_sha:
        _set("merge_intent_commit_sha", intent.commit_sha or pr.head_sha)
    return changed


#: Every ``PullRequest`` field :func:`apply_merge_intent` may touch, for
#: ``save(update_fields=...)`` callers.
MERGE_INTENT_FIELDS: tuple[str, ...] = (
    "merge_intent",
    "merge_intent_at",
    "merge_intent_source",
    "merge_intent_by",
    "merge_intent_excerpt",
    "merge_intent_commit_sha",
)


def classify_ci(check_runs: Iterable[dict[str, Any]], statuses: Iterable[dict[str, Any]]) -> str:
    """Fold a commit's check runs and commit statuses into one word.

    ``fail`` if anything failed, else ``pending`` if anything is still
    running, else ``pass`` if there was anything at all, else ``""`` — a
    repo with no CI is unknown, not green. Neutral and skipped runs are
    neither failures nor blockers.
    """
    saw_any = False
    pending = False
    failed = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
    for run in check_runs:
        saw_any = True
        conclusion = run.get("conclusion")
        if conclusion in failed:
            return "fail"
        if conclusion is None:
            pending = True
    for status in statuses:
        saw_any = True
        state = status.get("state")
        if state in {"failure", "error"}:
            return "fail"
        if state == "pending":
            pending = True
    if pending:
        return "pending"
    return "pass" if saw_any else ""
