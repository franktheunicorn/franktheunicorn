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

**Cheap and local, not an LLM pass.** Five hundred reports is 125,000 pairs. At a
model call per pair that is not a feature, it's a bill. So this is token overlap
plus a few structural signals, all computed from text already in the database. It
runs in well under a second for a backlog that size and costs nothing per report.
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

logger = logging.getLogger(__name__)

#: Identifier-ish words. Same shape as ``review.dedup``'s: what distinguishes two
#: security reports is the symbols and paths they name, and prose words in between
#: are noise that every report shares.
_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")

#: Paths and file:line references, which are the strongest cheap signal there is.
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
    #: Source paths mentioned anywhere. The strongest cheap signal.
    paths: frozenset[str] = frozenset()
    component: str = ""
    #: The scanner's own finding id, when there is one.
    finding_id: str = ""
    #: Which archive it came from, so two entries of the *same* scan can be told
    #: from the same entry appearing in two scans.
    source_archive: str = ""


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
        finding_id=(report.finding_id or "").strip(),
        source_archive=(report.source_archive or "").strip(),
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def score_pair(a: Signature, b: Signature, config: SecurityDuplicateConfig) -> Match:
    """How alike two reports are, and in what respects.

    A weighted blend rather than one number, because the signals fail in different
    places. Body overlap alone calls every finding in a scanner bundle a duplicate
    of every other, since they share the boilerplate the tool emits. Title alone
    misses the six-callers-of-one-helper case, where the titles name six different
    files. Paths alone tie together two genuinely different bugs that happen to
    live in the same large file.

    Two signals short-circuit to near-certainty because they are identity, not
    similarity: the same scanner ``finding_id`` from a *different* archive is
    literally the same finding re-scanned, and an identical title within one project
    is what a re-forwarded disclosure looks like.

    The finding-id guard needs a second check the title guard does not: the id is a
    per-archive sequence number (``f001``, ``f002``, …) for most scanners, not a
    stable hash, so ``f005`` in a branch-3.5 archive and ``f005`` in a main archive
    are the 5th finding in each scan — a coincidence, not the same hole. Requiring
    the titles to agree is what tells a genuine re-scan (same finding, same title)
    from a coincidental collision (same number, different bug). Without it a
    branch-3.5 finding and an unrelated main finding scored 1.00 on the id alone.
    """
    reasons: list[str] = []

    # Same finding, different scan. Not "similar" — the same tool ran twice on the
    # same code and numbered it the same. Guarded on the archive differing, because
    # within one archive the ids are unique and equality would mean comparing a row
    # with itself. Also guarded on the titles agreeing, because the id is a
    # per-archive sequence and a bare id match across two archives that scanned
    # *different* branches is a coincidence — see the note above.
    if (
        a.finding_id
        and a.finding_id == b.finding_id
        and a.source_archive != b.source_archive
        and config.trust_finding_id
        and a.title_tokens
        and a.title_tokens == b.title_tokens
    ):
        return Match(
            report_id=b.report_id,
            score=1.0,
            reasons=[
                f"same scanner finding id {a.finding_id!r} and identical title "
                f"in a different archive ({a.source_archive or 'unknown'} vs "
                f"{b.source_archive or 'unknown'})"
            ],
        )

    body = _jaccard(a.tokens, b.tokens)
    title = _jaccard(a.title_tokens, b.title_tokens)
    paths = _jaccard(a.paths, b.paths)

    if a.title_tokens and a.title_tokens == b.title_tokens and config.trust_identical_title:
        return Match(report_id=b.report_id, score=1.0, reasons=["identical title"])

    # ``trust_identical_title: false`` falls through to the weighted blend below
    # rather than short-circuiting to something else. It used to return raw ``body``,
    # which is not "stop treating an identical title as certainty" — it is "ignore the
    # title and path weights entirely". Measured: two reports with identical titles
    # *and* an identical source path scored on body alone, so they could come out
    # below a pair sharing only 0.67 of their titles. Discarding the path signal on a
    # flag about titles is not what the setting says it does.
    score = config.title_weight * title + config.body_weight * body + config.path_weight * paths
    total = config.title_weight + config.body_weight + config.path_weight
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


def detect_for_report(
    report: SecurityReport,
    config: SecurityDuplicateConfig,
) -> Detection:
    """Find and record a duplicate link for one report. Never raises.

    The triage-path entry point. Compares only against reports in the same project
    and only against **earlier** ones, nearest-first, up to ``max_candidates``.

    Both halves of that matter. Earlier-only keeps links pointing backwards in time,
    at the report carrying the accumulated triage — without it the window was simply
    "the newest N reports", which could point an older report at a newer one and, on
    a backlog past ``max_candidates``, excluded the genuine original precisely
    because it was old. The bound was trimming the wrong end.
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


def redetect_across_backlog(
    reports: Sequence[SecurityReport],
    config: SecurityDuplicateConfig,
) -> tuple[int, int]:
    """Re-run duplicate detection across *reports*: link new matches and clear
    stale auto-links that no longer score above the threshold.

    For the dashboard's "re-check duplicates" button — an explicit operator
    action, so it runs regardless of ``config.enabled`` (the flag gates the
    automatic triage-time path, not a button somebody pressed). Returns
    ``(linked, cleared)`` so the button can report both halves: a re-check that
    only ever added links would leave the false positives from a buggy heuristic
    sitting on the rows forever, which is the thing the operator pressed the
    button to clean up.

    Hand-set links (a NULL ``duplicate_confidence``) are never touched: a
    person's decision stays, even when the heuristic now disagrees. Only
    machine-made links (a recorded score) are reconsidered and cleared.

    Same quadratic shape as :func:`plan_duplicates`, for the same reason — set
    intersections over a few hundred tokens, so 500 reports is under a second.
    """
    ordered = sorted(reports, key=lambda r: (r.created_at, r.pk))
    signatures = [(report, build_signature(report)) for report in ordered]
    linked = 0
    cleared = 0
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
            if link_duplicate(report, best):
                linked += 1
        elif report.duplicate_of_id is not None and report.duplicate_confidence is not None:
            logger.info(
                "Re-check: clearing report #%s's stale duplicate link to #%s "
                "(no match above %.2f on re-eval).",
                report.pk,
                report.duplicate_of_id,
                config.threshold,
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
    return linked, cleared
