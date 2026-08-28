"""Bulk import of security reports from a zip archive.

The paste form takes one report at a time, which is fine for a trickle and
miserable for a backlog — an inbox export, a handover from a co-maintainer, a
year of reports someone finally got around to forwarding. This reads a zip of
report files and creates one :class:`SecurityReport` per entry, reusing the
same parsers as the email and paste paths so the recovered title/reporter are
identical however a report arrived.

Nothing is ever written to disk: entries are read into memory and stored in the
DB. That is deliberate, and it's what makes the classic zip attacks moot here —
there is no extraction step for a ``../../`` entry name or a symlink to escape
into.

What *is* still reachable is resource exhaustion, and three things bound it —
none of which trust the archive's own numbers:

* A codec allowlist. ZIP_DEFLATED honours the length argument to ``read``;
  bzip2 and lzma take CPython's unbounded ``decompress(data)`` branch, where a
  524-byte entry over a 512 MiB payload moved peak RSS by ~1 GB in 1.5s.
* Chunked reads, so each ``read`` call bounds its own decompression.
* The declared size, as a cheap *first* filter — a header that lies low doesn't
  buy anything, because CPython truncates every read to it and the CRC then
  fails.

The archive-wide budget is charged for rejected entries too; otherwise refusing
an entry is free and the aggregate cap bounds nothing.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import IO, TYPE_CHECKING

from django.db import transaction

from franktheunicorn.core.models import SecurityReport
from franktheunicorn.security.scan_archive import (
    MAX_FINDINGS,
    ScanArchive,
    expand_scan_archive,
)

if TYPE_CHECKING:
    from pathlib import Path

    from franktheunicorn.config.models import OperatorConfig
    from franktheunicorn.core.models import Project
    from franktheunicorn.data_access.email_inbox.types import InboxMessage

logger = logging.getLogger(__name__)

# Resource caps. A security-report archive is text; anything wildly outside
# these bounds is a mistake or an attack, and either way the operator wants to
# be told rather than have the box swap itself to death.
MAX_ENTRIES = 2_000
MAX_ENTRY_BYTES = 4 * 1024 * 1024  # 4 MiB of text is a very long report
MAX_TOTAL_BYTES = 128 * 1024 * 1024

# Single-message MIME, routed to the MIME parser. Deliberately just ".eml":
# ``parse_email_message`` parses exactly one message, so a ".mbox" would import
# an N-message export as ONE report with messages 2..N buried in the body of the
# first — invisible to the list, to triage and to dedup — and a real Outlook
# ".msg" is an OLE compound binary that would import as a report of OLE garbage.
# Handling those means splitting/decoding them properly, which this doesn't do,
# so it doesn't claim to. Everything textual goes to the paste parser instead.
_EML_SUFFIXES = frozenset({".eml"})

#: How many distinct security keywords an entry needs to count as a report.
#:
#: One, where the email door wants two. See the gate in ``_import_entry``: an
#: inbox is mostly not security reports and a false positive is cheap to ignore,
#: while an archive is hand-picked and a false *negative* silently loses a
#: finding.
_MIN_ZIP_KEYWORDS = 1

# Extensions never worth reading as a report. Anything not listed here is tried
# as text and vetoed by content sniffing if it turns out to be binary, so this
# only needs to cover formats whose *text-like* prefix would fool the sniffer.
_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".svg",
        ".ico",
        ".tiff",
        ".zip",
        ".gz",
        ".bz2",
        ".xz",
        ".tar",
        ".7z",
        ".rar",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".o",
        ".a",
        ".class",
        ".jar",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
        ".webm",
        ".pyc",
        ".pyo",
        ".wasm",
        ".mbox",
        ".msg",  # multi-message / OLE — see _EML_SUFFIXES above
        # A patch is a proposed *fix*, not a report, and scanner archives are
        # full of them — 124 of the 265 entries in the one that prompted this.
        # They're also prone to importing by accident: a diff carries its
        # surrounding context, so a hunk near the word "vulnerability" cleared the
        # keyword gate and became a "report" whose body is a patch.
        ".diff",
        ".patch",
    }
)


#: Cap on a reported entry name. ZIP filenames may be up to 64 KiB, and this one
#: is attacker-supplied and never lands in a column that would bound it — it goes
#: to logging and to Django's message storage, where eight 60 KB names spill ~480
#: KB past the cookie backend into the session store for a request anyone on the
#: Tailscale net can POST. Every other untrusted string on this path is already
#: truncated to its column width.
_MAX_ENTRY_NAME_LEN = 300


@dataclass(frozen=True)
class EntryOutcome:
    """What became of one entry in the archive."""

    name: str
    # "imported", "duplicate", "empty", "unsupported", "not-a-report",
    # "too-large", or "error"
    outcome: str
    report_id: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        # Truncated here rather than at each construction site, so a name that
        # reaches a log line or a flash message is bounded however it got here.
        if len(self.name) > _MAX_ENTRY_NAME_LEN:
            object.__setattr__(self, "name", self.name[:_MAX_ENTRY_NAME_LEN] + "…")


@dataclass
class ZipImportResult:
    """Everything the import looked at, so the operator can see the misses."""

    entries: list[EntryOutcome] = field(default_factory=list)
    queued_triage: int = 0
    #: Deep-verification runs queued by this import (``auto_verify``).
    queued_verifications: int = 0
    #: Why an explicitly requested verification didn't happen. Same reasoning as
    #: ``triage_skipped_reason``: the caller asked, so silence is a wrong answer.
    verify_skipped_reason: str = ""
    error: str = ""
    #: Why an explicitly requested triage didn't happen. Carried explicitly so
    #: callers don't have to guess a cause from ``queued_triage == 0`` and blame
    #: the operator's config for a bad parse.
    triage_skipped_reason: str = ""
    #: Things the operator should know that are not failures. ``error`` means "the
    #: import stopped"; the CLI turns it into a non-zero exit, so a cap that was
    #: reached after doing the work belongs here instead.
    warnings: list[str] = field(default_factory=list)
    #: The archive had more entries than the caller allowed. A flag, not a
    #: substring of ``error``: the dashboard used ``"over the" in result.error`` to
    #: decide whether to suggest the CLI, which breaks the moment anyone rewords
    #: either message.
    over_entry_cap: bool = False

    def _count(self, outcome: str) -> int:
        return sum(1 for e in self.entries if e.outcome == outcome)

    @property
    def imported(self) -> int:
        return self._count("imported")

    @property
    def duplicates(self) -> int:
        return self._count("duplicate")

    @property
    def skipped(self) -> int:
        """Entries deliberately passed over — not failures, just not reports."""
        return self._count("empty") + self._count("unsupported") + self._count("not-a-report")

    @property
    def failed(self) -> int:
        return self._count("error") + self._count("too-large")

    def summary(self) -> str:
        """One line for a flash message or a command's stdout."""
        if self.error and not self.imported:
            return f"Import failed: {self.error}"
        parts = [f"{self.imported} imported"]
        if self.duplicates:
            parts.append(f"{self.duplicates} already present")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.queued_triage:
            parts.append(f"{self.queued_triage} queued for triage")
        if self.queued_verifications:
            parts.append(f"{self.queued_verifications} queued for verification")
        parts.extend(self.warnings)
        if self.error:
            # A cap tripped mid-walk after rows were already committed. Saying
            # only "failed" hid N reports that are now in the operator's DB.
            parts.append(f"then stopped: {self.error}")
        return ", ".join(parts)


def import_reports_from_zip(
    source: str | Path | IO[bytes],
    *,
    project: Project | None = None,
    auto_triage: bool = False,
    auto_verify: bool = False,
    require_security_content: bool = True,
    max_entries: int = MAX_ENTRIES,
    archive_label: str = "",
) -> ZipImportResult:
    """Create a :class:`SecurityReport` for each report file in *source*.

    *source* is a path or any seekable binary file object, so this serves both
    the management command and the dashboard's upload button without either
    having to stage a temp file.

    ``auto_triage`` defaults to **False**, unlike every other ingest door, and
    that asymmetry is deliberate. One report arriving by email is one NVD lookup
    and two LLM calls, which is what ``security_triage.auto_triage`` was turned
    on for. A thousand-report backlog through the same switch is a thousand
    lookups and two thousand calls, charged the moment someone picks a file —
    the kind of bill you find out about afterwards. Bulk asks first.

    Even then it honours ``security_triage.enabled``, which the single-report
    door deliberately doesn't: a click is one report and the button is the
    consent, whereas this fans out over the whole archive, and CLAUDE.md wants a
    v1.5 path off unless config says otherwise. Refusing has to be *loud* — the
    setting defaults False and ships commented out, so a silent gate would make
    ``--triage`` a no-op on a default install. The reason lands in
    ``triage_skipped_reason`` for the caller to show.

    ``require_security_content`` applies the same filter the email door uses —
    the parser's own ``is_security_report`` verdict — and it matters more here
    than it looks. A directory-shaped handover archive contains a Makefile, a
    screenshot and, in the case that motivated this, an ``OPENSSH PRIVATE KEY``:
    all text, so content sniffing waves them through, each becoming a "report"
    that an operator may then send to an LLM. Set it False to import everything
    textual regardless.

    ``auto_verify`` queues the deep verifier (``security.verifier``) for every
    report that imported. Opt-in for the same reason as ``auto_triage`` and more
    so: triage is two LLM calls a report, verification is a full agent run *per
    active branch* on a real checkout. Queued after the walk rather than inside
    it, because a verification needs nothing from the per-entry loop and threading
    a second flag through five functions to arrive at the same place is how the
    two get out of step.

    ``max_entries`` lets a caller ask for a tighter bound than ``MAX_ENTRIES``.
    Neither door does any more — the dashboard used to, on the grounds that the
    whole import runs inside one HTTP request, which the single-transaction walk
    made cheap enough not to matter.

    Never raises for a bad archive: a corrupt or non-zip file comes back as
    ``result.error`` so the caller can show it. Per-entry problems are recorded
    against the entry and the rest of the archive still imports.
    """
    result = ZipImportResult()
    # Recorded on every row for provenance. Derived from a path when the caller
    # didn't name it; the dashboard passes the upload's filename, which is the
    # only name a browser upload has.
    if not archive_label and isinstance(source, str | PurePath):
        archive_label = PurePath(source).name
    archive_label = archive_label[:255]
    # Loaded once here rather than per entry: get_operator_config re-reads and
    # re-validates the YAML on every call, and a config edited mid-import would
    # otherwise apply to some entries and not others.
    operator_config = None
    if auto_triage:
        try:
            from franktheunicorn.config.loader import get_operator_config

            operator_config = get_operator_config()
        except Exception as exc:
            # Fail closed: without the config we can't tell whether the operator
            # switched triage off, and guessing wrong costs two LLM calls a report.
            logger.warning("Could not load operator config; importing untriaged", exc_info=True)
            result.triage_skipped_reason = (
                f"could not read the operator config ({str(exc) or exc.__class__.__name__})"
            )
            auto_triage = False
        else:
            if not operator_config.security_triage.enabled:
                logger.info("security_triage.enabled is false; importing untriaged")
                result.triage_skipped_reason = "security_triage.enabled is false in operator.yaml"
                auto_triage = False
            elif not operator_config.llm_backends:
                # The dashboard button checks this; neither bulk door did. Queueing
                # anyway created one command per report that failed the moment the
                # worker picked it up, while summary() reported "N queued for
                # triage" as though it were success — 2000 of them for a big
                # archive.
                logger.info("No LLM backend configured; importing untriaged")
                result.triage_skipped_reason = "no LLM backend is configured in operator.yaml"
                auto_triage = False
    try:
        with zipfile.ZipFile(source) as archive:
            _import_entries(
                archive,
                project,
                auto_triage,
                result,
                operator_config,
                require_security_content,
                max_entries,
                archive_label,
            )
    except zipfile.BadZipFile:
        result.error = "not a valid zip archive"
    except Exception as exc:
        logger.exception("Security report zip import failed")
        result.error = str(exc) or exc.__class__.__name__
        # The walk runs in one transaction, so anything escaping it rolled every
        # row back — including the ones already recorded as "imported" with a
        # report_id. Reporting those would tell the operator about reports that do
        # not exist ("4 imported, then stopped: database is locked" with an empty
        # table) and hand the CLI and the dashboard the same fiction. Reconcile to
        # what the database actually holds.
        _discard_uncommitted_outcomes(result)
        return result

    if auto_verify:
        # After the transaction, deliberately. A command queued for a row that then
        # rolled back is a foreign key to nothing, and the failure mode is the
        # worker picking up work for a report the operator never got.
        _queue_verifications(result)
    return result


def _queue_verifications(result: ZipImportResult) -> None:
    """Queue the deep verifier for everything that imported.

    Honours ``verifier.enabled`` and says so when it declines, because this is
    reached by an explicit ``--verify``/checkbox: an operator who asked for it and
    got silence would reasonably conclude the flag was broken rather than that a
    setting they've never seen is false.
    """
    ids = [entry.report_id for entry in result.entries if entry.outcome == "imported"]
    ids = [report_id for report_id in ids if report_id]
    if not ids:
        return

    try:
        from franktheunicorn.config.loader import get_operator_config

        verifier = get_operator_config().security_triage.verifier
    except Exception as exc:
        logger.warning("Could not load operator config; importing unverified", exc_info=True)
        result.verify_skipped_reason = (
            f"could not read the operator config ({str(exc) or exc.__class__.__name__})"
        )
        return
    if not verifier.enabled:
        logger.info("security_triage.verifier.enabled is false; importing unverified")
        result.verify_skipped_reason = "security_triage.verifier.enabled is false in operator.yaml"
        return

    from franktheunicorn.core.models import SecurityReport
    from franktheunicorn.security.queue import PRIORITY_BULK, queue_verification

    unattached = 0
    for report in SecurityReport.objects.filter(pk__in=ids).select_related("project"):
        if report.project_id is None:
            # No repo to check against. Counted rather than queued, because the
            # worker would only reach the same conclusion minutes later, once per
            # report, and log it where nobody is looking.
            unattached += 1
            continue
        if queue_verification(report, priority=PRIORITY_BULK):
            result.queued_verifications += 1
    if unattached:
        result.verify_skipped_reason = (
            f"{unattached} report(s) have no project, so there is no repo to verify them "
            "against — re-import with a project selected"
        )
    logger.info(
        "Queued %d verification(s) from this import%s",
        result.queued_verifications,
        f" ({unattached} skipped for having no project)" if unattached else "",
    )


def _discard_uncommitted_outcomes(result: ZipImportResult) -> None:
    """Rewrite committed-looking outcomes after a rollback.

    Only ``imported`` and ``duplicate`` are rewritten: a duplicate points at a row
    from a previous import, which is still there, but its *outcome for this run* is
    no longer "we checked and it was present" — the check happened in the
    transaction that vanished. Skips and failures were decisions about the entry,
    not writes, so they stand.
    """
    rewritten = [
        EntryOutcome(name=entry.name, outcome="error", detail="rolled back with the transaction")
        if entry.outcome in ("imported", "duplicate")
        else entry
        for entry in result.entries
    ]
    result.entries[:] = rewritten
    result.queued_triage = 0


def _import_entries(
    archive: zipfile.ZipFile,
    project: Project | None,
    auto_triage: bool,
    result: ZipImportResult,
    operator_config: OperatorConfig | None,
    require_security_content: bool,
    max_entries: int,
    archive_label: str,
) -> None:
    """Walk the archive in name order, recording an outcome for every entry."""
    candidates = [info for info in archive.infolist() if not _is_ignorable(info)]
    if len(candidates) > max_entries:
        result.error = f"archive has {len(candidates)} entries, over the {max_entries} limit"
        result.over_entry_cap = True
        return

    # Built once, up front. The obvious spelling — a filter() per entry against
    # raw_text — is a full scan comparing whole report bodies, for every entry,
    # which turns the bulk case this exists to serve into an O(entries x rows)
    # crawl. Hashing the table once is one pass, and it dedups *within* the
    # archive for free as newly created rows land in the same index.
    seen = _build_dedup_index()

    # One transaction for the whole walk. Measured on this box, file-backed
    # SQLite: 265 entries cost 1008ms as individual commits and 6.3ms inside one
    # (160x); 2000 entries, 8.3s against 27ms. Parsing is 0.03ms an entry, so the
    # write lock the worker contends for is held ~0.1s rather than acquired and
    # released 2000 times. This is what made the web door's separate entry cap
    # unnecessary.
    #
    # The savepoint inside _import_entry is not an optimisation: a caught
    # database error inside an atomic block leaves the transaction unusable, and
    # that except clause is load-bearing (keep-what-imported on a locked DB).
    # A scanner archive says what its findings are; take its word over the file
    # layout. Detection is all-or-nothing per archive and yields nothing for an
    # ordinary folder of reports, so the generic walk below is unchanged for
    # everything that isn't a scan bundle.
    #
    # Through a budgeted reader, not a bare lambda. The expansion pass reads every
    # .json and every patch/note, and handing it a lambda that dropped
    # _read_entry's byte count meant MAX_TOTAL_BYTES simply did not apply to it:
    # measured, a 163 KB archive of 40 four-megabyte entries decompressed 160 MiB
    # in 0.49s, and at the dashboard's 8 MB upload cap that extrapolates to ~7.8
    # GiB inside one web request. The reader also caches, so an entry the expander
    # inspects and rejects isn't inflated a second time by the walk below.
    reader = _BudgetedReader(archive)
    expanded = expand_scan_archive(archive, reader)
    if reader.exhausted:
        result.error = f"archive expands past the {MAX_TOTAL_BYTES} byte total limit"
        return
    with transaction.atomic():
        if expanded.recognised:
            _import_findings(
                expanded,
                project,
                auto_triage,
                result,
                seen,
                operator_config,
                archive_label,
            )
        # Whatever the expander consumed is skipped here: importing the manifest
        # as well would store the same 129 findings twice, once split and once as
        # a single unreadable blob.
        #
        # A rollup it only *partly* consumed is a third case, and it stays in the
        # walk with its text overridden: TRIAGE.md's 126 per-finding sections are
        # now on the findings, but its summary tables and panel-dispute notes are
        # about the run and would be lost if the file were dropped whole.
        _walk_entries(
            archive,
            [i for i in candidates if i.filename not in expanded.consumed],
            project,
            auto_triage,
            result,
            seen,
            operator_config,
            require_security_content,
            archive_label,
            {name: text.encode("utf-8") for name, text in expanded.residuals.items()},
        )


class _BudgetedReader:
    """Reads entries once, and charges every byte against the archive budget.

    Two jobs the expansion pass needed and a lambda couldn't do. It reads every
    ``.json`` before deciding whether it's a manifest and every patch/note in a
    bundle directory, so without the budget the aggregate cap covered only the
    generic walk, and without the cache each rejected entry was inflated twice —
    once here, once by the walk that never saw it marked consumed.

    Returns None for a refused entry, which the expander already treats as "skip
    this one", so a bomb degrades to "not recognised as a scan archive" rather
    than to an unbounded read.
    """

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self._archive = archive
        self._cache: dict[str, bytes | None] = {}
        self.produced = 0
        self.exhausted = False

    def __call__(self, info: zipfile.ZipInfo) -> bytes | None:
        if info.filename in self._cache:
            return self._cache[info.filename]
        if self.exhausted:
            return None
        data, _outcome, _detail, produced = _read_entry(self._archive, info)
        self.produced += produced
        if self.produced > MAX_TOTAL_BYTES:
            self.exhausted = True
            logger.warning(
                "Scan expansion hit the %d byte archive budget at %s",
                MAX_TOTAL_BYTES,
                info.filename,
            )
            self._cache[info.filename] = None
            return None
        self._cache[info.filename] = data
        return data


def _import_findings(
    expanded: ScanArchive,
    project: Project | None,
    auto_triage: bool,
    result: ZipImportResult,
    seen: dict[str, int],
    operator_config: OperatorConfig | None,
    archive_label: str,
) -> None:
    """Create one report per expanded finding, patch attached.

    Dedup runs on the rendered text exactly as it does for a file, so a re-import
    of the same archive is still a no-op — the rendering is deterministic.

    Rows are created in the archive's own priority order, highest first, and that
    is load-bearing rather than cosmetic: the worker claims WorkerCommands by
    ``created_at``, so with ``--triage`` the insertion order *is* the order the
    findings get triaged in. On the archive that prompted this, arrival order put
    the two HIGHs at positions 3 and 94 of 129.
    """
    project_id = project.pk if project is not None else None
    ranked = sorted(expanded.findings, key=lambda r: (-r.priority, r.finding_id))
    for record in ranked:
        text_key = _text_key(record.body, project_id)
        existing = seen.get(text_key)
        if existing is None and project_id is not None:
            # The same project-less fallback _import_entry does. Without it, the
            # findings path alone re-imported on a second pass with --project:
            # measured, three findings became six rows while the plain text file
            # in the same archive correctly deduped — half the archive honouring
            # the page's "re-importing is safe" hint and half not.
            existing = seen.get(_text_key(record.body, None))
        if existing is not None:
            _record(
                result,
                EntryOutcome(name=record.origin_label, outcome="duplicate", report_id=existing),
            )
            continue
        try:
            with transaction.atomic():
                report = SecurityReport.objects.create(
                    raw_text=record.body,
                    title=record.title[:500],
                    project=project,
                    source="zip",
                    source_archive=archive_label,
                    finding_id=record.finding_id,
                    proposed_patch=record.patch,
                    proposed_patch_path=record.patch_path[:500],
                    priority=record.priority,
                    priority_reason=record.priority_reason[:200],
                )
        except Exception as exc:
            logger.warning("Could not store finding %s", record.finding_id, exc_info=True)
            _record(
                result, EntryOutcome(name=record.origin_label, outcome="error", detail=str(exc))
            )
            continue

        seen[text_key] = report.pk
        _record(
            result,
            EntryOutcome(name=record.origin_label, outcome="imported", report_id=report.pk),
        )
        if auto_triage:
            from franktheunicorn.security.queue import queue_triage_on_request

            try:
                if queue_triage_on_request(report, operator_config):
                    result.queued_triage += 1
            except Exception:
                logger.warning("Could not queue triage for finding %s", record.finding_id)

    if expanded.truncated:
        # A warning on the result, not result.error. The command raises
        # CommandError on result.error, so putting it there exited 1 on a run that
        # committed every row it meant to and simply stopped at the cap — any cron
        # or CI wrapper reading the status treated a working import as a permanent
        # failure.
        result.warnings.append(
            f"manifest claimed more than {MAX_FINDINGS} findings; expanded the first {MAX_FINDINGS}"
        )


def _walk_entries(
    archive: zipfile.ZipFile,
    candidates: list[zipfile.ZipInfo],
    project: Project | None,
    auto_triage: bool,
    result: ZipImportResult,
    seen: dict[str, int],
    operator_config: OperatorConfig | None,
    require_security_content: bool,
    archive_label: str,
    overrides: dict[str, bytes],
) -> None:
    """Record an outcome for every entry, in name order.

    *overrides* replaces an entry's bytes with text the scan expander already
    produced (a rollup with its per-finding sections lifted out). Those bytes were
    charged against the archive budget when the expander read them, so they are
    not charged again here — but every name, codec and size check still runs,
    because they are checks about the *entry*, not about what we do with it.
    """
    read_bytes = 0
    for info in sorted(candidates, key=lambda i: i.filename):
        # Name-based rejections FIRST, before a single byte is decompressed.
        # Doing this inside _import_entry meant an archive of screenshots paid
        # full decompression for every one of them and could trip the aggregate
        # cap having imported nothing — and once it tripped, the entry that
        # tripped it and every entry after it got no EntryOutcome at all, so the
        # counts didn't add up to the archive.
        if _suffix(info.filename) in _BINARY_SUFFIXES:
            _record(
                result,
                EntryOutcome(
                    name=info.filename, outcome="unsupported", detail="unhandled file type"
                ),
            )
            continue

        # Encryption before the codec test: a password-protected archive fails
        # every entry's CRC and came back as N copies of "could not read entry",
        # which reads like a corrupt file. Bit 0 of the general-purpose flags says
        # so up front, and no password is ever going to be supplied here.
        if info.flag_bits & 0x1:
            _record(
                result,
                EntryOutcome(
                    name=info.filename,
                    outcome="unsupported",
                    detail="encrypted entry — unzip it first, this importer takes no password",
                ),
            )
            continue

        # Codec first: an entry we will never read shouldn't be reported by size.
        if info.compress_type not in _SAFE_COMPRESS_TYPES:
            _record(
                result,
                EntryOutcome(
                    name=info.filename,
                    outcome="unsupported",
                    detail=f"unsupported compression (type {info.compress_type})",
                ),
            )
            continue

        # A cheap rejection for *honest* headers. Not the whole defence — the
        # central directory belongs to whoever built the archive — but it is a
        # real bound in its own right: CPython truncates every read to this
        # declared size (`data = data[:self._left]`), and a header that lies low
        # then fails the CRC check. _read_entry adds the bound that does not
        # depend on the header at all.
        if info.file_size > MAX_ENTRY_BYTES:
            _record(
                result,
                EntryOutcome(
                    name=info.filename,
                    outcome="too-large",
                    detail=f"{info.file_size} bytes exceeds the {MAX_ENTRY_BYTES} byte limit",
                ),
            )
            continue

        override = overrides.get(info.filename)
        # Read branch first, so ``raw`` is inferred as the ``bytes | None`` the
        # checks below still need to handle rather than being narrowed to bytes by
        # the override branch.
        if override is None:
            raw, outcome, detail, produced = _read_entry(archive, info)
        else:
            raw, outcome, detail, produced = override, "imported", "", 0

        # Charged even when the entry was rejected. Skipping this on the failure
        # paths meant a rejected entry decompressed for free, so an archive of
        # entries that each blow the per-entry cap cost unbounded aggregate work
        # while the aggregate cap read zero.
        read_bytes += produced
        if raw is None:
            _record(result, EntryOutcome(name=info.filename, outcome=outcome, detail=detail))
            if read_bytes > MAX_TOTAL_BYTES:
                result.error = f"archive expands past the {MAX_TOTAL_BYTES} byte total limit"
                return
            continue

        if read_bytes > MAX_TOTAL_BYTES:
            _record(
                result,
                EntryOutcome(
                    name=info.filename,
                    outcome="too-large",
                    detail="archive total-size budget exhausted",
                ),
            )
            result.error = f"archive expands past the {MAX_TOTAL_BYTES} byte total limit"
            return

        _record(
            result,
            _import_entry(
                info,
                raw,
                project,
                auto_triage,
                result,
                seen,
                operator_config,
                # A residual is exempt from the keyword gate, and not as a
                # convenience: the gate asks "does this look like a security
                # report?" because a handover folder also contains a Makefile and
                # once contained a private key. An entry the scan expander
                # recognised, split, and handed back the remainder of is already
                # known to be part of a scan archive, so the question is answered.
                # Asking it anyway dropped PATCHES/composition.md — its
                # per-finding sections had moved out and the apply-order narrative
                # left behind names no vulnerability, so a file that imported
                # fine before this change came back "not-a-report".
                require_security_content and override is None,
                archive_label,
            ),
        )


#: Codecs whose decompression CPython will bound for us. ZIP_DEFLATED honours the
#: length argument to ``read`` (``decompress(data, n)`` in ``_read1``); ZIP_STORED
#: does no decompression at all. Every other codec — bzip2, lzma — takes the
#: ``else`` branch, ``decompress(data)`` with no ``max_length``, expanding
#: everything the compressed read fed it before the declared-size truncation is
#: applied. Measured: a 524-byte bzip2 entry over a 512 MiB payload moved peak RSS
#: by ~1 GB in 1.5s. No security report needs bzip2, so they are refused.
_SAFE_COMPRESS_TYPES = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

#: Read granularity. Each ``read(n)`` on a deflate member bounds that call's
#: decompression to roughly n, so the loop stops one chunk past the cap rather
#: than materialising whatever the entry really holds.
_READ_CHUNK_BYTES = 256 * 1024


def _read_entry(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo
) -> tuple[bytes | None, str, str, int]:
    """Decompress one entry, bounded, without trusting its header.

    Returns ``(data, outcome, detail, bytes_produced)``. ``data`` is None when the
    entry was refused, and ``bytes_produced`` is what was actually decompressed
    either way so the caller can charge it against the archive-wide budget.

    ``archive.read(info)`` is the obvious call and it does not bound anything:
    CPython decompresses in ~1 GiB slices and truncates to the declared
    ``file_size`` only afterwards. Reading in capped chunks bounds the work for
    the codecs that honour a length argument, and codecs that don't are refused.

    Both guards below are *invariant checks, not the live defence* — the caller
    screens compress_type and file_size before calling, so neither can fire
    today, and coverage says so. They stay because this function's contract is
    "bounded", and a future caller that skips the pre-screen shouldn't silently
    get an unbounded read. Don't read them as the thing standing between you and
    a zip bomb; that's the codec allowlist and the chunk size.
    """
    if info.compress_type not in _SAFE_COMPRESS_TYPES:  # pragma: no cover - caller screens
        return None, "unsupported", f"unsupported compression (type {info.compress_type})", 0

    produced = 0
    chunks: list[bytes] = []
    try:
        with archive.open(info) as handle:
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                produced += len(chunk)
                if produced > MAX_ENTRY_BYTES:  # pragma: no cover - caller screens
                    return (
                        None,
                        "too-large",
                        f"decompressed past the {MAX_ENTRY_BYTES} byte limit "
                        f"(header claimed {info.file_size})",
                        produced,
                    )
                chunks.append(chunk)
    except Exception:
        # A header that under-reports the size fails CPython's CRC check here, as
        # does an ordinary corrupt entry. Both are "skip it and say so".
        logger.debug("Could not read zip entry %s", info.filename, exc_info=True)
        return None, "error", "could not read entry", produced

    return b"".join(chunks), "imported", "", produced


def _build_dedup_index() -> dict[str, int]:
    """Map every stored report to a dedup key, in one pass over the table.

    Two key shapes share the dict: ``mid:<project-id>:<message-id>`` for
    anything that arrived with one, and ``txt:<project-id>:<sha256>`` for
    everything else. Both carry the project, and both carry the *row's own*
    project rather than the import target — so the same report filed under two
    projects stays two reports, and a re-import doesn't treat half an archive as
    already present. Streamed with ``iterator()`` so a big report table isn't
    pulled into memory at once.
    """
    index: dict[str, int] = {}
    rows = SecurityReport.objects.values_list(
        "pk", "email_message_id", "raw_text", "project_id"
    ).iterator()
    for pk, message_id, raw_text, project_id in rows:
        if message_id:
            index.setdefault(_mid_key(message_id, project_id), pk)
        index.setdefault(_text_key(raw_text or "", project_id), pk)
    return index


#: Width of ``SecurityReport.email_message_id``. Keys are built from the
#: truncated value because that's what the column holds — computing the key from
#: the full one made message-id dedup a guaranteed miss for any id longer than
#: this, so every re-import added another copy and another triage bill.
_MESSAGE_ID_MAX_LEN = 500


def _record(result: ZipImportResult, outcome: EntryOutcome) -> None:
    """Record an entry's outcome, and log it when the entry didn't become a report.

    The one place that happens, so "see the worker log" is true for every outcome
    the dashboard summarises rather than names. Nothing logged for an import or a
    duplicate — those are visible in the list itself.
    """
    result.entries.append(outcome)
    if outcome.outcome in ("imported", "duplicate"):
        return
    logger.info(
        "Zip import skipped %s: %s%s",
        outcome.name,
        outcome.outcome,
        f" ({outcome.detail})" if outcome.detail else "",
    )


def _mid_key(message_id: object, project_id: int | None) -> str:
    # str() before slicing: compat32 wraps a header carrying a raw non-ASCII byte
    # in an email.header.Header, which is truthy and stringifies but isn't
    # subscriptable.
    scope = project_id if project_id is not None else "none"
    return f"mid:{scope}:{str(message_id)[:_MESSAGE_ID_MAX_LEN]}"


def _text_key(body: str, project_id: int | None) -> str:
    digest = hashlib.sha256(body.strip().encode("utf-8", errors="replace")).hexdigest()
    return f"txt:{project_id if project_id is not None else 'none'}:{digest}"


def _import_entry(
    info: zipfile.ZipInfo,
    raw: bytes,
    project: Project | None,
    auto_triage: bool,
    result: ZipImportResult,
    seen: dict[str, int],
    operator_config: OperatorConfig | None,
    require_security_content: bool,
    archive_label: str,
) -> EntryOutcome:
    name = info.filename
    kind = _classify(name, raw)
    if kind == "binary":
        # Decided on content, not on the name. An extension test alone let an
        # ELF, a JPEG and an OPENSSH PRIVATE KEY through as "reports" — the key
        # then sitting in the reports table waiting to be posted to an LLM.
        return EntryOutcome(name=name, outcome="unsupported", detail="not a text file")
    if kind == "unsupported":
        return EntryOutcome(name=name, outcome="unsupported", detail="unhandled file type")

    try:
        parsed = _parse_entry(raw, kind)
    except Exception as exc:
        logger.debug("Could not parse zip entry %s", name, exc_info=True)
        return EntryOutcome(name=name, outcome="error", detail=str(exc))

    body = parsed.body.strip()
    if not body:
        return EntryOutcome(name=name, outcome="empty", detail="no report text")

    if require_security_content and len(parsed.matched_keywords) < _MIN_ZIP_KEYWORDS:
        # Deliberately NOT parsed.is_security_report, which is the email door's
        # verdict and wants two distinct keywords. That threshold is right for an
        # inbox, where most mail isn't a security report and a false positive
        # costs an LLM call on someone's lunch order. An archive the operator
        # hand-picked and pointed at this importer inverts the base rate, and the
        # cost of being strict there is losing a real finding: measured against a
        # real scanner archive, PATCHES/bug_03/notes.md says "vulnerability" once
        # and was dropped, while two patch.diffs got in on incidental keywords in
        # their context lines. One keyword still catches what this gate exists
        # for — a Makefile or a PEM private key, which match nothing.
        return EntryOutcome(name=name, outcome="not-a-report", detail="no security keywords found")

    project_id = project.pk if project is not None else None
    text_key = _text_key(body, project_id)
    # Scoped to the target project like the text key. A Message-ID is globally
    # unique, but keying on it globally while keying text per-project meant a
    # re-import with --project refused the .eml entries as "already present" and
    # duplicated the .txt ones — half the archive honouring the operator's
    # intent and half not.
    mid_key = _mid_key(parsed.message_id, project_id) if parsed.message_id else ""
    existing = seen.get(mid_key) if mid_key else None
    if existing is None:
        existing = seen.get(text_key)
    if existing is None and project_id is not None:
        # Fall back to the project-less keys. The email door creates every report
        # with project=None (worker.runner never passes one), so scoping strictly
        # to the import target meant `--project owner/repo` on an inbox export
        # duplicated every message the poller had already ingested — with
        # --triage, a second NVD lookup and second pair of LLM calls each. Which
        # is the exact workflow this importer exists to serve.
        existing = seen.get(_mid_key(parsed.message_id, None)) if parsed.message_id else None
        if existing is None:
            existing = seen.get(_text_key(body, None))
    if existing is not None:
        return EntryOutcome(name=name, outcome="duplicate", report_id=existing)

    # Fall back to the file name for the title: an operator scanning the list
    # would rather see "2024-03-cve-request.txt" than "Untitled Report".
    #
    # Stores the *stripped* body, which is what the paste form does and what the
    # dedup key is computed over — storing the raw one instead made the
    # text-based dedup miss on every re-import, since the archived file's
    # trailing newline never matched.
    #
    # Every CharField is truncated to its max_length, as triage.py does for the
    # same fields: a 600-char display name in a .eml is silently over-long on
    # SQLite and a hard DataError on the Postgres path DATABASE_URL enables.
    try:
        # A savepoint, not decoration: the caller holds one transaction for the
        # whole archive, and catching a database error inside it without one
        # leaves the transaction unusable for every later entry.
        with transaction.atomic():
            report = SecurityReport.objects.create(
                raw_text=body,
                title=(str(parsed.subject) or _title_from_name(name))[:500],
                project=project,
                # str() before slicing, on all of them. Python's compat32 email
                # policy wraps any header carrying a raw non-ASCII byte in an
                # email.header.Header, which is truthy, stringifies fine, and is
                # not subscriptable — so slicing it raised TypeError and turned a
                # legitimate .eml report into a silent per-entry error.
                reporter_name=str(parsed.from_name)[:255],
                reporter_email=str(parsed.from_email)[:255],
                source="zip",
                source_archive=archive_label,
                # Same bound _mid_key uses, or the stored value and the dedup key
                # disagree and the message-id door stops working.
                email_message_id=str(parsed.message_id)[:_MESSAGE_ID_MAX_LEN],
                email_received_at=parsed.received_at,
            )
    except Exception as exc:
        # The only unguarded call in the loop was this one, so a DataError or a
        # "database is locked" (web and worker share one SQLite file) escaped to
        # the catch-all, abandoned the remaining entries, and reported the whole
        # archive as failed.
        logger.warning("Could not store report from zip entry %s", name, exc_info=True)
        return EntryOutcome(name=name, outcome="error", detail=str(exc))

    # Register immediately: two copies of the same report inside one archive
    # should collapse the same way a re-import does.
    seen[text_key] = report.pk
    if mid_key:
        seen.setdefault(mid_key, report.pk)

    if auto_triage:
        from franktheunicorn.security.queue import queue_triage_on_request

        try:
            # _on_request, not _if_enabled: reaching here means the operator
            # passed --triage or ticked the box, which is an explicit ask and
            # shouldn't be vetoed by the "triage things as they arrive" setting.
            if queue_triage_on_request(report, operator_config):
                result.queued_triage += 1
        except Exception:
            # An import that lands the reports but can't queue them is still a
            # good import — the operator can click Triage.
            logger.warning(
                "Could not queue triage for imported report %d", report.pk, exc_info=True
            )

    return EntryOutcome(name=name, outcome="imported", report_id=report.pk)


def _classify(name: str, raw: bytes) -> str:
    """Decide how to read an entry: ``"eml"``, ``"text"``, ``"binary"``, ``"unsupported"``.

    Extension first, then content — and content gets the veto. Extensions alone
    were wrong in both directions: ``_suffix`` returns the fragment after the
    last dot, so a mail export named after its subject line
    (``Re: [SECURITY] traversal in v1.2.3``) classified as ``.3`` and got dropped
    as an unsupported type, while anything extensionless was waved through as
    text however binary it actually was.
    """
    suffix = _suffix(name)
    if suffix in _BINARY_SUFFIXES:
        return "unsupported"
    if _looks_binary(raw):
        return "binary"
    if suffix in _EML_SUFFIXES:
        return "eml"
    # Anything textual is worth a try, including an unrecognised or accidental
    # extension — the parser tolerates whatever it gets, and mail exports carry
    # names nobody can enumerate in advance.
    return "text"


#: Byte-order marks and what they mean. Longest first — the UTF-32-LE mark
#: starts with the UTF-16-LE one, so testing short-first misreads every UTF-32
#: file as UTF-16.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xef\xbb\xbf", "utf-8"),
)


def _bom(raw: bytes) -> tuple[bytes, str] | None:
    """The BOM at the head of *raw* and its encoding, if there is one."""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return bom, encoding
    return None


def _looks_binary(raw: bytes) -> bool:
    """Whether *raw* is binary, by the NUL heuristic git and file(1) both use.

    UTF-16/32 holds a NUL for every ASCII character, so BOM-marked bytes are
    decoded first and the check runs on the result. Accepting them on the BOM
    alone waved through any binary that happened to start ``\\xff\\xfe`` — two
    bytes, no scan — which git wouldn't and file(1) wouldn't.
    """
    marker = _bom(raw)
    if marker is None:
        return b"\x00" in raw[:8192]
    bom, encoding = marker
    # An *incremental* decoder with final=False, not bytes.decode(). The slice
    # boundary lands wherever 8192 bytes land, which is not a character boundary:
    # a UTF-8 BOM is 3 bytes, so any multi-byte character straddling the cut made
    # a strict decode raise and a perfectly good BOM-marked report — Notepad's
    # default save — was dropped as "not a text file". final=False buffers the
    # incomplete tail instead of calling it an error, while a genuinely malformed
    # sequence earlier in the slice still raises, which is the signal we want.
    try:
        head = codecs.getincrementaldecoder(encoding)().decode(raw[len(bom) : 8192], final=False)
    except UnicodeDecodeError:
        return True
    return "\x00" in head


def _parse_entry(raw: bytes, kind: str) -> InboxMessage:
    """Route an entry to the MIME parser or the paste parser.

    Both return an ``InboxMessage``, which is the point: a report imported from
    a zip ends up with the same recovered metadata as the same report arriving
    by email or pasted into the form.
    """
    from franktheunicorn.data_access.email_inbox.parser import (
        parse_email_message,
        parse_pasted_report,
    )

    if kind == "eml":
        # Only the BOM is dealt with here; everything else goes to the MIME parser
        # as raw bytes. Blanket-decoding as UTF-8 with errors="replace" and
        # re-encoding fixed the two BOM cases and broke a third thing: a part
        # declaring charset=iso-8859-1 with 8-bit encoding had every 0x80-0xFF
        # byte turned into U+FFFD *before* email could apply that charset, so
        # "café" reached the operator and the triage prompt as "caf<?>". RFC2047
        # headers survived, which made the corruption silent and partial —
        # precisely the common case for non-English mail.
        return parse_email_message(_strip_bom_bytes(raw))
    return parse_pasted_report(_decode_text(raw))


def _strip_bom_bytes(raw: bytes) -> bytes:
    """Byte-level BOM handling for a MIME message, preserving its own encodings.

    * No BOM: untouched, so per-part charsets still work.
    * UTF-8 BOM: the three marker bytes removed. They sit where the first header
      name goes, so the parser treated the whole message as a body and lost
      subject, From, Date and Message-ID — while still importing, which is why
      nothing flagged it.
    * UTF-16/32 BOM: transcoded to UTF-8, because the entire message really is in
      that encoding and ``email.message_from_bytes`` cannot read a header out of
      it — the report came back ``is_security_report=False`` and vanished as
      "not-a-report" with its keywords intact but unfindable.
    """
    marker = _bom(raw)
    if marker is None:
        return raw
    bom, encoding = marker
    body = raw[len(bom) :]
    if encoding == "utf-8":
        return body
    return body.decode(encoding, errors="replace").encode("utf-8")


def _decode_text(raw: bytes) -> str:
    """Decode entry bytes to text, honouring and stripping a BOM.

    ``errors="replace"`` rather than failing: a report is worth importing with a
    mangled byte in it, and mail exports carry all sorts of encodings.

    NULs are dropped. ``_looks_binary`` only scans the first 8 KiB, so a report
    with a core-dump or log paste below that mark keeps its NULs — valid UTF-8,
    so ``errors="replace"`` doesn't touch them, and they carry no meaning in a
    text report. They also make the two database backends disagree: SQLite stores
    them, psycopg refuses a text parameter containing one outright, so the
    Postgres path ``DATABASE_URL`` enables would fail an entry the default path
    imports. (Unmeasured — psycopg isn't installed here — but the divergence is
    documented and stripping costs nothing either way.) And they'd otherwise ride
    into the triage prompt.
    """
    marker = _bom(raw)
    if marker is not None:
        bom, encoding = marker
        text = raw[len(bom) :].decode(encoding, errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\x00", "")


def _is_ignorable(info: zipfile.ZipInfo) -> bool:
    """Directory entries and archiver bookkeeping, not reports.

    Splits on backslash as well as forward slash. The spec says forward, but
    plenty of Windows tooling writes ``__MACOSX\\._x`` style names anyway, and
    both checks here are on path segments — so a backslash archive skipped the
    dotfile test *and* the resource-fork test and imported the junk as reports.
    """
    if info.is_dir():
        return True
    parts = info.filename.replace("\\", "/").split("/")
    base = parts[-1]
    if not base or base.startswith("."):  # .DS_Store, ._resource forks
        return True
    # Bare "__MACOSX" only: splitting on "/" can't yield a segment containing one,
    # so the old "__MACOSX/" arm of this test was unreachable.
    return "__MACOSX" in parts


def _suffix(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[-1].lower()


def _title_from_name(name: str) -> str:
    """The entry's path, not its basename.

    Scanner archives put the distinguishing part in the directory:
    ``PATCHES/bug_57/meta.json``. Titling from the basename gave a real archive
    four reports called "notes.md" and 124 called "meta.json", which is a list
    the operator can't navigate. Trailing separator stripped so a stray one
    doesn't produce an empty title.
    """
    return name.replace("\\", "/").strip("/") or name
