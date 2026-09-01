"""Is this report the same hole as one already in the backlog?

Triage asks whether a report is plausible. The verifier asks whether it's real in
the code. This asks the question that only matters once you have a *pile* of
reports: have I already got this one?

It matters because of how the reports arrive. A scanner archive produces one
finding per site, so the same missing check in a shared helper comes back as six
findings against six callers. Two scans of the same repo produce two copies of
everything. A public disclosure gets forwarded by three different people. Working
through several hundred of those in batches, the expensive mistake isn't
mis-triaging one report, it's investigating the same hole four times and fixing it
four times — or worse, fixing it once and leaving three open rows that look like
live vulnerabilities.

**Titles to the model, not pairs.** Five hundred reports is 125,000 pairs, and a
model call per pair is a bill rather than a feature. But the question "which of
these titles describe the same hole?" is one a model can answer for a whole
project in a single call, titles being short — so the batch pass sends the
backlog's titles a few hundred at a time and gets groups back. The token-overlap
heuristic remains as the no-backend path and the management command's default,
with one signal removed and one added. Removed: the scanner's ``finding_id``, a
per-archive sequence number rather than an identity (see ``score_pair`` for why
it was decoration). Added: the proposed patch, when both reports have one —
compared as a line-level edit distance with the hunk headers' line numbers and
the context lines discarded, because that is where the same fix against master
and against branch-3.5 drifts, and an identical patch is the same finding
re-scanned.

The point is to surface a *candidate link* for a human to glance at, which is what
was asked for — not to make a ruling.

**It links, it does not judge.** Nothing here sets ``status="duplicate"``. That's a
verdict, verdicts are the operator's, and a heuristic that silently marked reports
duplicate would be a heuristic that silently hides vulnerabilities. The link, the
score and the reason go on the row; the decision stays with the person reading it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from franktheunicorn.config.models import SecurityDuplicateConfig
    from franktheunicorn.core.models import SecurityReport
    from franktheunicorn.review.backends.base import BaseLLMBackend

logger = logging.getLogger(__name__)

#: Identifier-ish words. Same shape as ``review.dedup``'s: what distinguishes two
#: security reports is the symbols and paths they name, and prose words in between
#: are noise that every report shares.
_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")

#: Paths and file:line references, the strongest of the text signals.
#: Two reports naming ``core/src/main/scala/…/Utils.scala`` are about the same code
#: whatever words they wrapped it in.
_PATH_RE = re.compile(r"[\w./-]+\.(?:java|scala|py|js|ts|go|rb|c|cc|cpp|h|rs|kt|xml|yaml|yml)")

#: Words that appear in essentially every security report and therefore separate
#: nothing. Left deliberately short — an aggressive stop list starts throwing away
#: the vocabulary that distinguishes a deserialization bug from an XSS one.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "this",
        "that",
        "with",
        "from",
        "was",
        "are",
        "can",
        "could",
        "would",
        "not",
        "but",
        "you",
        "your",
        "have",
        "has",
        "any",
        "all",
        "may",
        "when",
        "which",
        "there",
        "then",
        "than",
        "vulnerability",
        "security",
        "issue",
        "report",
        "attacker",
        "impact",
        "severity",
        "description",
        "summary",
        "poc",
        "proof",
        "concept",
        "steps",
        "reproduce",
    }
)

#: How much of each text field to read. A scanner entry can be enormous and the
#: distinguishing content is at the top; reading all of it makes every report in a
#: bundle look alike because they share the same trailing boilerplate.
_MAX_TEXT_CHARS = 4000


@dataclass(frozen=True)
class Signature:
    """The comparable shape of one report. Cheap to build, cheap to compare."""

    report_id: int
    #: Distinguishing words from title, component, impact and the head of the body.
    tokens: frozenset[str] = frozenset()
    #: Just the title's words, weighted separately: two reports with the same title
    #: are duplicates far more often than two reports with similar bodies.
    title_tokens: frozenset[str] = frozenset()
    #: Source paths mentioned anywhere.
    paths: frozenset[str] = frozenset()
    component: str = ""
    #: The proposed patch's changed lines, normalized (see ``_patch_lines``).
    #: Empty when the report has no patch — a paste has nothing to compare.
    patch_lines: tuple[str, ...] = ()


@dataclass
class Match:
    """A candidate duplicate, with the arithmetic that produced it."""

    report_id: int
    score: float
    #: Human-readable "why", stored on the row. A bare 0.72 is not something an
    #: operator can check, and this feature only works if they check it.
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _tokens(text: str) -> frozenset[str]:
    words = _WORD_RE.findall((text or "")[:_MAX_TEXT_CHARS].lower())
    return frozenset(w for w in words if w not in _STOPWORDS)


def build_signature(report: SecurityReport) -> Signature:
    """Everything needed to compare *report*, read off fields already loaded."""
    title = report.title or ""
    body = f"{report.raw_text or ''}\n{report.parsed_poc or ''}\n{report.parsed_impact or ''}"
    haystack = f"{title}\n{report.parsed_component or ''}\n{body}"
    return Signature(
        report_id=report.pk,
        tokens=_tokens(haystack),
        title_tokens=_tokens(title),
        paths=frozenset(_PATH_RE.findall(haystack[:_MAX_TEXT_CHARS].lower())),
        component=(report.parsed_component or "").strip().lower(),
        patch_lines=_patch_lines(report.proposed_patch or ""),
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


#: Below this many changed lines an "identical patch" is a coincidence, not an
#: identity — two unrelated findings can both add ``import os``.
_MIN_IDENTITY_PATCH_LINES = 3

#: Cell budget for the line-level edit distance. Past it the pair falls back to
#: its line-set overlap — the exact ordering of a 600-line patch's lines is not
#: where the signal lives, and the sweep is quadratic in the backlog already.
_MAX_EDIT_CELLS = 250_000

#: A file header is ``--- a/x`` / ``+++ b/x`` / ``/dev/null`` — three signs and
#: a path. A bare ``---``/``+++`` prefix is not enough: inside a hunk that line
#: is payload (a removed SQL ``-- comment``, an added ``++i``), and dropping it
#: can manufacture an "identical patch" out of two that differ.
_FILE_HEADER_RE = re.compile(r"^(?:---|\+\+\+) (?:a/|b/|/dev/null)")


def _patch_lines(patch: str) -> tuple[str, ...]:
    """The patch's payload: its added/removed lines, line numbers discarded.

    A unified diff's ``@@ -12,4 +12,5 @@`` headers and context lines are where
    the same fix against a different branch drifts — the code above it moved —
    so neither is compared. What remains is the change itself, whitespace-
    normalized, which is the part that is identical when the same finding is
    scanned against master and branch-3.5.

    Assumes git-style patches, which is what the scanner archives carry: a
    plain ``diff -u`` header (``--- file.orig``) becomes payload, adding one
    noise line rather than dropping a real one.
    """
    lines = []
    for line in patch.splitlines():
        if _FILE_HEADER_RE.match(line):
            continue  # file headers: paths, not payload
        if line[:1] in ("+", "-"):
            lines.append(f"{line[0]}{line[1:].strip()}")
    return tuple(lines)


def _line_edit_similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """Levenshtein over lines, as 1 - distance/length. What it buys over a set
    overlap is the payload that drifted by a line or two — a re-generated fix
    against code that moved since the last scan. 0.0 when either report has no
    patch to compare."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if not frozenset(a) & frozenset(b):
        # Zero shared lines means distance == max length means similarity 0 —
        # exactly, no DP needed. This is the prefilter that keeps the quadratic
        # sweep cheap: two different findings' patches almost never share a line.
        return 0.0
    if len(a) * len(b) > _MAX_EDIT_CELLS:
        return _jaccard(frozenset(a), frozenset(b))
    # One-row DP; the strings are lines, so a cell is a line comparison.
    previous = list(range(len(b) + 1))
    for i, line_a in enumerate(a, 1):
        current = [i]
        for j, line_b in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (line_a != line_b))
            )
        previous = current
    return 1.0 - previous[-1] / max(len(a), len(b))


def score_pair(a: Signature, b: Signature, config: SecurityDuplicateConfig) -> Match:
    """How alike two reports are, and in what respects.

    A weighted blend rather than one number, because the signals fail in different
    places. Body overlap alone calls every finding in a scanner bundle a duplicate
    of every other, since they share the boilerplate the tool emits. Title alone
    misses the six-callers-of-one-helper case, where the titles name six different
    files. Paths alone tie together two genuinely different bugs that happen to
    live in the same large file.

    Two signals short-circuit to certainty because they are identity, not
    similarity: an identical title within one project is what a re-forwarded
    disclosure looks like, and an identical patch (line numbers ignored) is the
    same finding re-scanned, possibly against another branch. The scanner's
    ``finding_id`` is deliberately *not* such a signal — it is a per-archive
    sequence number (``f001``, ``f002``, …), so the same id in two archives is
    the 5th finding of each scan, a coincidence rather than the same hole. It
    used to short-circuit here (guarded on identical titles) and that guard is
    the whole story: the title was doing the work, the id was decoration.
    """
    reasons: list[str] = []

    body = _jaccard(a.tokens, b.tokens)
    title = _jaccard(a.title_tokens, b.title_tokens)
    paths = _jaccard(a.paths, b.paths)
    patch = _line_edit_similarity(a.patch_lines, b.patch_lines)

    if a.title_tokens and a.title_tokens == b.title_tokens and config.trust_identical_title:
        return Match(report_id=b.report_id, score=1.0, reasons=["identical title"])

    if (
        config.trust_identical_patch
        and len(a.patch_lines) >= _MIN_IDENTITY_PATCH_LINES
        and sorted(a.patch_lines) == sorted(b.patch_lines)
    ):
        return Match(report_id=b.report_id, score=1.0, reasons=["identical patch"])

    # ``trust_identical_title: false`` falls through to the weighted blend below
    # rather than short-circuiting to something else. It used to return raw ``body``,
    # which is not "stop treating an identical title as certainty" — it is "ignore the
    # title and path weights entirely". Measured: two reports with identical titles
    # *and* an identical source path scored on body alone, so they could come out
    # below a pair sharing only 0.67 of their titles. Discarding the path signal on a
    # flag about titles is not what the setting says it does.
    score = config.title_weight * title + config.body_weight * body + config.path_weight * paths
    total = config.title_weight + config.body_weight + config.path_weight
    if a.patch_lines and b.patch_lines:
        # Only compared when both have one: a pasted report has no patch, and
        # penalizing it for that is how a paste never matches its own scan.
        score += config.patch_weight * patch
        total += config.patch_weight
    score = score / total if total else 0.0

    # A shared component is corroboration, not evidence on its own: on a project
    # like Spark half the backlog is "core". So it nudges rather than scores.
    if a.component and a.component == b.component:
        score = min(1.0, score + config.same_component_bonus)
        reasons.append(f"same component {a.component!r}")

    if title:
        reasons.append(f"title overlap {title:.2f}")
    if body:
        reasons.append(f"text overlap {body:.2f}")
    if paths:
        shared = sorted(a.paths & b.paths)[:3]
        reasons.append(f"shares {', '.join(shared)}")
    if patch:
        reasons.append(f"patch overlap {patch:.2f}")

    return Match(report_id=b.report_id, score=round(score, 3), reasons=reasons)


def find_duplicate(
    subject: SecurityReport,
    candidates: Iterable[SecurityReport],
    config: SecurityDuplicateConfig,
) -> Match | None:
    """The best above-threshold match for *subject*, or None.

    *candidates* is the caller's business — it should already be scoped to the same
    project, since a Spark report cannot duplicate a Kafka one and comparing across
    projects is both wrong and quadratic in the whole table.

    Ties break towards the **earlier** report. That is the one that already has the
    triage, the verification rows and possibly the operator's notes on it, so it is
    the one worth keeping as canonical; pointing the older row at the newer would
    move the accumulated work to the wrong end of the link.
    """
    subject_sig = build_signature(subject)
    best: Match | None = None
    for candidate in candidates:
        if candidate.pk == subject.pk:
            continue
        match = score_pair(subject_sig, build_signature(candidate), config)
        if match.score < config.threshold:
            continue
        if (
            best is None
            or match.score > best.score
            # Tie: the lower id is the earlier report, which is the one carrying the
            # accumulated triage and therefore the one to keep as canonical.
            or (match.score == best.score and match.report_id < best.report_id)
        ):
            best = match
    return best


def resolve_canonical(report: SecurityReport, limit: int = 10) -> SecurityReport:
    """Follow ``duplicate_of`` to the end of the chain.

    B duplicating A and then C duplicating B should leave C pointing at A, not at a
    row that is itself a pointer — otherwise the detail page shows "duplicate of a
    duplicate" and the operator has to walk it by hand. *limit* is a cycle guard: the
    write path refuses to create one, but a hand-edit through the Django admin can,
    and an infinite loop in the worker is a worse outcome than a slightly wrong link.
    """
    seen = {report.pk}
    current = report
    for _ in range(limit):
        nxt = current.duplicate_of
        if nxt is None or nxt.pk in seen:
            break
        seen.add(nxt.pk)
        current = nxt
    return current


def would_create_cycle(subject: SecurityReport, target: SecurityReport, limit: int = 10) -> bool:
    """Whether pointing *subject* at *target* closes a loop.

    Reachable in ordinary use, not a theoretical worry: re-running detection over a
    backlog compares every report against every other, so A→B on one pass and B→A on
    the next is exactly what an unguarded implementation does.
    """
    seen = {target.pk}
    current: SecurityReport | None = target
    for _ in range(limit):
        current = current.duplicate_of if current else None
        if current is None:
            return False
        if current.pk == subject.pk:
            return True
        if current.pk in seen:
            return False
        seen.add(current.pk)
    return False


def link_duplicate(subject: SecurityReport, match: Match) -> bool:
    """Record *match* on *subject*. True if the row changed.

    Writes three fields and deliberately not ``status``: marking a report
    ``duplicate`` is a verdict, and a heuristic making verdicts is a heuristic that
    hides vulnerabilities. The link is a pointer for the operator to check.

    **Never overwrites a link the operator made.** A ``duplicate_of`` with a NULL
    ``duplicate_confidence`` is somebody's decision — detection always records a
    score — and this used to walk straight over it: a hand-linked report was
    silently repointed at whatever scored highest, at confidence 1.0. Two other
    places in the codebase already asserted this invariant in their own docstrings
    (``triage._check_duplicates`` and the ``--relink`` help text) while nothing
    enforced it, which is how it went unnoticed.
    """
    from franktheunicorn.core.models import SecurityReport as Report

    if subject.duplicate_of_id is not None and subject.duplicate_confidence is None:
        logger.debug(
            "Leaving report #%s's hand-set duplicate link to #%s alone.",
            subject.pk,
            subject.duplicate_of_id,
        )
        return False

    target = Report.objects.filter(pk=match.report_id).first()
    if target is None or target.pk == subject.pk:
        return False

    target = resolve_canonical(target)
    if target.pk == subject.pk or would_create_cycle(subject, target):
        logger.debug(
            "Not linking report #%s to #%s: it would close a duplicate cycle.",
            subject.pk,
            target.pk,
        )
        return False

    if (
        subject.duplicate_of_id == target.pk
        and abs((subject.duplicate_confidence or 0.0) - match.score) < 0.001
    ):
        return False

    subject.duplicate_of = target
    subject.duplicate_confidence = match.score
    subject.duplicate_reason = match.reason[:500]
    # updated_at is auto_now, and Django only applies that to fields named in
    # update_fields — so omitting it leaves the timestamp stale. That is not
    # cosmetic here: sheet_sync's staleness guard refuses "a row whose report
    # changed after the export", and without this a duplicate link is invisible to
    # it, so a stale spreadsheet edit wins over it. _check_cves next door already
    # gets this right.
    subject.save(
        update_fields=["duplicate_of", "duplicate_confidence", "duplicate_reason", "updated_at"]
    )
    logger.info(
        "Report #%s looks like a duplicate of #%s (score %.2f: %s)",
        subject.pk,
        target.pk,
        match.score,
        match.reason,
    )
    return True


@dataclass
class Detection:
    """What a detection attempt did, as three distinguishable outcomes.

    A bare ``Match | None`` conflated "the check ran and found nothing" with "the
    check never ran", and the caller acts differently on each: the first is grounds
    to clear a stale link, the second is not. Collapsing them meant switching the
    feature *off* deleted every existing link on the next re-triage — and logged it
    as "this run found no match above the threshold (0.62)", a negative result
    reported by a check that never happened.
    """

    #: True only when reports were actually compared.
    ran: bool = False
    match: Match | None = None
    #: Why it didn't run. Empty when it did.
    declined: str = ""


# --------------------------------------------------------------------------- #
# The LLM pass
# --------------------------------------------------------------------------- #

#: Titles per model call in the batch sweep. Titles are short, so a few hundred
#: fit one context easily — the bound exists so a 5,000-report backlog doesn't
#: blow the window, not because the model loses the thread at 301.
_MAX_LLM_TITLES_PER_CALL = 300

#: What the model's word for its confidence is worth as a stored score. The
#: number's job is to mark the link as machine-made (a hand-set link has a NULL
#: confidence) and to sort sanely — it is not a measurement.
_LLM_CONFIDENCE_SCORES = {"high": 0.95, "medium": 0.8, "low": 0.65}

_LLM_SYSTEM_PROMPT = (
    "You are grouping security vulnerability reports about ONE software project. "
    "Two reports are duplicates when they describe the same underlying "
    "vulnerability — the same weakness in the same component — even when worded "
    "differently, filed by different scanners, or forwarded by different people. "
    "Different vulnerabilities that happen to live in the same file are NOT "
    "duplicates. A scanner's finding id (f001, bug_7, …) is a per-archive "
    "sequence number, not an identity: the same id in two scans is a coincidence "
    "unless the titles say otherwise, and one report mentioning another's id in "
    "its text is a cross-reference inside one archive, not a duplicate copy of "
    "it. Answer ONLY with JSON."
)

_LLM_USER_INSTRUCTIONS = (
    "\n\nWhich of these reports describe the same underlying vulnerability? "
    'Return {"groups": [{"ids": [12, 37], "confidence": "high"|"medium"|"low", '
    '"reason": "one sentence"}]} with only groups of two or more, every id from '
    'the list above. If none are duplicates, return {"groups": []}.'
)


@dataclass(frozen=True)
class LLMGroup:
    """One duplicate set the model called out, oldest report first."""

    ids: tuple[int, ...]
    confidence: str
    reason: str


@dataclass
class LLMSweep:
    """What the model said about one backlog slice, plus what it saw.

    ``chunks`` records which reports were in the same call, because that is what
    makes a *negative* result meaningful: the model can only be said to have
    declined to group two titles it actually saw together. Clearing a stale link
    on "the model didn't group them" is only honest for pairs in one chunk.
    """

    groups: list[LLMGroup]
    chunks: list[frozenset[int]]


def _render_titles(reports: Sequence[SecurityReport]) -> str:
    """The numbered title list the model groups. Component included — "Unvalidated
    redirect" in core and in the web UI are different bugs with the same title."""
    lines = []
    for report in reports:
        title = (report.title or report.raw_text[:120]).replace("\n", " ")[:200]
        component = f" [{report.parsed_component[:60]}]" if report.parsed_component else ""
        lines.append(f"#{report.pk}{component} {title}")
    return "\n".join(lines)


def _parse_llm_groups(data: object, order: dict[int, int]) -> list[LLMGroup]:
    """The model's answer as groups, oldest-first, each report in at most one.

    *order* maps report id to its position in the chunk (which is
    arrival-ordered), so a group's canonical report is the earliest one — the row
    carrying the accumulated triage. Ids the model invented, and reports already
    claimed by an earlier group, drop out; a group that shrinks below two is not
    a group.
    """
    if not isinstance(data, dict):
        return []
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        return []
    groups: list[LLMGroup] = []
    claimed: set[int] = set()
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        ids = raw.get("ids")
        if not isinstance(ids, list):
            continue
        known = sorted(
            {
                report_id
                for report_id in ids
                if isinstance(report_id, int) and report_id in order and report_id not in claimed
            },
            key=order.__getitem__,
        )
        if len(known) < 2:
            continue
        claimed.update(known)
        confidence = str(raw.get("confidence") or "low").lower()
        if confidence not in _LLM_CONFIDENCE_SCORES:
            confidence = "low"
        reason = str(raw.get("reason") or "")[:300]
        groups.append(LLMGroup(ids=tuple(known), confidence=confidence, reason=reason))
    return groups


def llm_duplicate_sweep(
    reports: Sequence[SecurityReport],
    backend: BaseLLMBackend,
    *,
    project_id: int | None = None,
) -> LLMSweep | None:
    """Ask the model which of *reports* describe the same hole. None when every call failed.

    One metered call per ``_MAX_LLM_TITLES_PER_CALL`` reports — the whole point is
    that "group these titles" is a single question, not one per pair. A chunk that
    fails (model down, unparseable answer) is skipped with a warning: its pairs
    are simply absent from the result, so nothing linked by an earlier run gets
    cleared on the strength of a call that never happened. ``None` — rather than
    an empty sweep — when *no* chunk produced an answer, so the caller can tell
    "the model found no duplicates" apart from "the model could not be asked".
    """
    from franktheunicorn.security.triage import _safe_json_parse

    ordered = sorted(reports, key=lambda r: (r.created_at, r.pk))
    chunks = [
        ordered[i : i + _MAX_LLM_TITLES_PER_CALL]
        for i in range(0, len(ordered), _MAX_LLM_TITLES_PER_CALL)
    ]
    groups: list[LLMGroup] = []
    chunk_ids: list[frozenset[int]] = []
    failed = 0
    for chunk in chunks:
        order = {report.pk: position for position, report in enumerate(chunk)}
        user_message = _render_titles(chunk) + _LLM_USER_INSTRUCTIONS
        try:
            raw = backend.metered_call(
                _LLM_SYSTEM_PROMPT,
                user_message,
                action_type="security-duplicates",
                project_id=project_id,
            )
            data = _safe_json_parse(raw)
        except Exception:
            failed += 1
            logger.warning(
                "LLM duplicate grouping call failed for a %d-report chunk; those "
                "reports are untreated by this sweep.",
                len(chunk),
                exc_info=True,
            )
            continue
        if data is None:
            failed += 1
            logger.warning(
                "LLM duplicate grouping returned nothing parseable for a %d-report "
                "chunk; those reports are untreated by this sweep.",
                len(chunk),
            )
            continue
        # Only an answered chunk goes in: the clear guard reads "both ends were
        # in a chunk" as "the model saw both titles and declined", and a chunk
        # the model never answered is not a negative result.
        chunk_ids.append(frozenset(report.pk for report in chunk))
        groups.extend(_parse_llm_groups(data, order))
    if chunks and failed == len(chunks):
        return None
    return LLMSweep(groups=groups, chunks=chunk_ids)


def _group_match(group: LLMGroup, subject_id: int) -> Match | None:
    """The link a group implies for one of its members, pointing at the oldest."""
    canonical_id = next((report_id for report_id in group.ids if report_id != subject_id), None)
    if canonical_id is None:
        return None
    return Match(
        report_id=canonical_id,
        score=_LLM_CONFIDENCE_SCORES[group.confidence],
        reasons=[f"LLM ({group.confidence}): {group.reason}"[:490]],
    )


def _detect_via_llm(
    report: SecurityReport,
    candidates: Sequence[SecurityReport],
    backend: BaseLLMBackend,
) -> Detection:
    """The triage-time duplicate check: the report's title against the backlog's.

    Same question the batch sweep asks, scoped to "is *this* one already here".
    Falls to ``declined`` rather than to the heuristic when the model can't be
    asked — two mechanisms writing the same field is how the finding-id mess
    happened, and a missed link is recoverable (the re-check button) while a
    wrong one is a hidden vulnerability.
    """
    sweep = llm_duplicate_sweep([report, *candidates], backend, project_id=report.project_id)
    if sweep is None:
        return Detection(declined="the LLM duplicate check failed (see the log)")
    if not any(report.pk in chunk for chunk in sweep.chunks):
        # The subject's own chunk failed while another answered: "no group
        # contains it" below would be a negative result from a comparison the
        # report was never part of, and Detection.ran exists to keep that from
        # clearing an existing link.
        return Detection(declined="the LLM duplicate check failed for this report's chunk")
    for group in sweep.groups:
        if report.pk not in group.ids:
            continue
        match = _group_match(group, report.pk)
        if match is None:
            continue
        link_duplicate(report, match)
        return Detection(ran=True, match=match)
    logger.info(
        "LLM duplicate check found no duplicate for report #%s among %d earlier "
        "report(s) in the same project.",
        report.pk,
        len(candidates),
    )
    return Detection(ran=True)


def detect_for_report(
    report: SecurityReport,
    config: SecurityDuplicateConfig,
    backend: BaseLLMBackend | None = None,
) -> Detection:
    """Find and record a duplicate link for one report. Never raises.

    The triage-path entry point. Compares only against reports in the same project
    and only against **earlier** ones, nearest-first, up to ``max_candidates``.

    Both halves of that matter. Earlier-only keeps links pointing backwards in time,
    at the report carrying the accumulated triage — without it the window was simply
    "the newest N reports", which could point an older report at a newer one and, on
    a backlog past ``max_candidates``, excluded the genuine original precisely
    because it was old. The bound was trimming the wrong end.

    With *backend* the comparison is the LLM title pass; without it, the local
    heuristic. Triage always has a backend — it could not have run otherwise —
    so the heuristic path is for the management command and no-backend installs.
    """
    from franktheunicorn.core.models import SecurityReport as Report

    if not config.enabled:
        logger.debug("Duplicate detection is off (security_triage.duplicates.enabled)")
        return Detection(declined="security_triage.duplicates.enabled is false")
    if report.project_id is None:
        # Not scoped to anything. Comparing against every report of every project
        # would produce links across unrelated codebases.
        logger.info(
            "Report #%s has no project, so duplicate detection has nothing to scope "
            "a comparison to; skipping.",
            report.pk,
        )
        return Detection(declined="the report has no project")

    candidates = list(
        Report.objects.filter(project_id=report.project_id, created_at__lt=report.created_at)
        .exclude(pk=report.pk)
        .order_by("-created_at")[: config.max_candidates]
    )
    if not candidates:
        logger.debug("Report #%s is the earliest in its project; nothing to compare.", report.pk)
        # Ran, in the sense that matters to the caller: there was nothing to match
        # against, so a link from a previous run is stale and should go.
        return Detection(ran=True)

    if backend is not None:
        return _detect_via_llm(report, candidates, backend)

    match = find_duplicate(report, candidates, config)
    if match is None:
        # Logged as explicitly as a hit: "no duplicate found" and "the check never
        # ran" must not look the same, per the rule in CLAUDE.md.
        logger.info(
            "No duplicate found for report #%s among %d earlier report(s) in the same "
            "project (threshold %.2f).",
            report.pk,
            len(candidates),
            config.threshold,
        )
        return Detection(ran=True)

    # The match is reported whether or not the write happened. A refusal from
    # link_duplicate means "already recorded", "that would close a cycle", or "the
    # operator set this by hand" — none of which is a reason to clear anything.
    link_duplicate(report, match)
    return Detection(ran=True, match=match)


def detect_across_backlog(
    reports: Sequence[SecurityReport],
    config: SecurityDuplicateConfig,
) -> int:
    """Link duplicates across a whole set of reports. Returns how many were linked.

    For the backfill command, and for the case that motivated this: several hundred
    reports already imported before the feature existed.

    Quadratic on purpose, and affordable — the comparison is set intersections over
    a few hundred tokens, so 500 reports (125,000 pairs) is under a second. Making
    it an inverted index would be faster and would also be the third thing to go
    wrong in a feature whose job is to be a hint.

    Processed oldest-first so links point backwards in time, which is what makes the
    canonical report the one carrying the accumulated triage.
    """
    return sum(
        1 for subject, match in plan_duplicates(reports, config) if link_duplicate(subject, match)
    )


def plan_duplicates(
    reports: Sequence[SecurityReport],
    config: SecurityDuplicateConfig,
) -> list[tuple[SecurityReport, Match]]:
    """The (report, best-match) pairs a sweep would consider linking.

    Split out so the command's dry run and its ``--apply`` cannot disagree. They had
    two copies of this loop, and the copies diverged the moment ``link_duplicate``
    grew a guard: the dry run printed ``#2 -> #1`` for a report whose link the
    operator had set by hand, and ``--apply`` then correctly refused it and reported
    "Linked 0". The database ended up right and the preview lied, which for a
    read-only preview is the whole of its job.

    Pairs are *candidates*, not promises — ``link_duplicate`` still has the final say
    on cycles, hand-set links and no-op rewrites. The caller renders that honestly by
    running the same function.

    Quadratic on purpose, and affordable: the comparison is set intersections over a
    few hundred tokens, so 500 reports (125,000 pairs) is under a second. An inverted
    index would be faster and would also be the third thing to go wrong in a feature
    whose job is to be a hint.
    """
    ordered = sorted(reports, key=lambda r: (r.created_at, r.pk))
    signatures = [(report, build_signature(report)) for report in ordered]
    planned: list[tuple[SecurityReport, Match]] = []
    for index, (report, signature) in enumerate(signatures):
        best: Match | None = None
        for earlier, earlier_sig in signatures[:index]:
            if earlier.project_id != report.project_id:
                continue
            match = score_pair(signature, earlier_sig, config)
            if match.score < config.threshold:
                continue
            if best is None or match.score > best.score:
                best = match
        if best is not None:
            planned.append((report, best))
    return planned


def would_link(subject: SecurityReport, match: Match) -> bool:
    """Whether :func:`link_duplicate` would actually write this pair.

    The read-only half of ``link_duplicate``'s guards, so a dry run can show what an
    ``--apply`` will really do. Kept next to it rather than in the command, because
    the two going out of step is the bug this exists to prevent.
    """
    from franktheunicorn.core.models import SecurityReport as Report

    if subject.duplicate_of_id is not None and subject.duplicate_confidence is None:
        return False  # the operator's own link
    target = Report.objects.filter(pk=match.report_id).first()
    if target is None or target.pk == subject.pk:
        return False
    target = resolve_canonical(target)
    return not (target.pk == subject.pk or would_create_cycle(subject, target))


def bucket_by_project(
    reports: Sequence[SecurityReport],
) -> dict[int | None, list[SecurityReport]]:
    """Group reports by project for the sweep — a Spark report cannot duplicate a
    Kafka one; projectless reports form their own bucket.

    Shared by :func:`redetect_across_backlog` and the management command's
    ``--llm`` dry run, so the preview buckets exactly what ``--apply`` will.
    """
    buckets: dict[int | None, list[SecurityReport]] = {}
    for report in reports:
        buckets.setdefault(report.project_id, []).append(report)
    return buckets


def redetect_across_backlog(
    reports: Sequence[SecurityReport],
    config: SecurityDuplicateConfig,
    backend: BaseLLMBackend,
) -> tuple[int, int] | None:
    """Re-run duplicate detection across *reports* with the LLM title pass.

    For the dashboard's "re-check duplicates" button — an explicit operator
    action, so it runs regardless of ``config.enabled`` (the flag gates the
    automatic triage-time path, not a button somebody pressed). Reports are
    grouped per project (a Spark report cannot duplicate a Kafka one; projectless
    reports form their own bucket) and each bucket's titles go to the model in
    chunks. Returns ``(linked, cleared)``, or ``None`` when no chunk produced an
    answer at all, so the button can say "the model couldn't be asked" rather
    than "0 linked".

    Two halves, both by the model's say-so, and in this order:

    * **Clear** a stale auto-link only when the model saw both titles in the same
      answered call and did not group them. A link whose other end was never
      shown to the model — a different chunk, a different project, a report
      outside this set — is left alone: "not grouped" is only an answer when the
      question was asked.
    * **Link** every member of each group the model called out, at the group's
      canonical (oldest) report. ``link_duplicate`` still has the final word on
      cycles and hand-set links.

    Clear runs first because ``link_duplicate`` resolves a new link's canonical
    through existing ones: written first, a group member's link would chain
    through the stale link the clear half is about to delete, and the run would
    end with the model's own group unlinked — "1 linked, 1 cleared" for a pair
    the model called out.

    Hand-set links (a NULL ``duplicate_confidence``) are never touched: a
    person's decision stays, even when the model disagrees.
    """
    ordered = sorted(reports, key=lambda r: (r.created_at, r.pk))
    by_id = {report.pk: report for report in ordered}
    buckets = bucket_by_project(ordered)

    linked = 0
    cleared = 0
    swept = False
    any_answered = False
    for project_id, bucket in buckets.items():
        if len(bucket) < 2:
            # Nothing to compare it against here, so nothing to affirm or clear.
            continue
        swept = True
        sweep = llm_duplicate_sweep(bucket, backend, project_id=project_id)
        if sweep is None:
            # Logged there. This bucket's links stay exactly as they are.
            continue
        any_answered = True

        group_of: dict[int, int] = {}
        for index, group in enumerate(sweep.groups):
            for report_id in group.ids:
                group_of[report_id] = index

        for report in bucket:
            if report.duplicate_of_id is None or report.duplicate_confidence is None:
                continue  # unlinked, or a link the operator set by hand
            other = report.duplicate_of_id
            if group_of.get(report.pk) is not None and group_of.get(report.pk) == group_of.get(
                other
            ):
                continue  # the model affirmed this pair
            if not any(report.pk in chunk and other in chunk for chunk in sweep.chunks):
                continue  # never shown to the model together — no answer to act on
            logger.info(
                "Re-check: clearing report #%s's stale duplicate link to #%s "
                "(the model saw both titles and did not group them).",
                report.pk,
                other,
            )
            report.duplicate_of = None
            report.duplicate_confidence = None
            report.duplicate_reason = ""
            report.save(
                update_fields=[
                    "duplicate_of",
                    "duplicate_confidence",
                    "duplicate_reason",
                    "updated_at",
                ]
            )
            cleared += 1

        for group in sweep.groups:
            for member_id in group.ids[1:]:
                match = _group_match(group, member_id)
                if match is not None and link_duplicate(by_id[member_id], match):
                    linked += 1

    if swept and not any_answered:
        return None
    return linked, cleared
