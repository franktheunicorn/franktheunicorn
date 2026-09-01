"""Expand a scanner archive into one report per finding.

``zip_import`` treats an archive as a bag of files and makes one report per file.
That is right for a folder of forwarded emails and wrong for the output of an
automated scanner, which is the shape a maintainer actually gets handed. A real
one looked like this:

    VULN-FINDINGS.json      {"findings": [129 dicts], "target": ..., "commit": ...}
    TRIAGE.json             {"findings": [129 dicts]}   # a panel's take, same ids
    CRITICAL-CANDIDATES.json{"findings": [2 dicts]}     # a CVSS overlay, same ids
    PATCHES.json            {"patches": [{"id": "bug_01", "finding": "f001", ...}]}
    VULN-FINDINGS.md        the same 129 findings as prose, one "## f001 — …" each
    TRIAGE.md               ditto, "### f003 — …"
    MAINTAINER-REPORT.md    a 343 KB rollup, per-finding bullets inside clusters
    review_verdicts.txt     "bug_94: ACCEPT | style=5 | …" one line per patch
    PATCHES/bug_01/meta.json  {"finding": "f001", "title": ..., "severity": ...}
    PATCHES/bug_01/patch.diff  the proposed fix
    ...                     124 more of those

File-per-report gave 143 reports out of that: 129 findings and then fourteen
rollups, of which four were 200-350 KB blobs holding *the same findings again* —
nobody can triage a 343 KB file, and its f003 paragraph is worth reading only
next to f003. So everything per-finding is de-normalised onto the finding:

* a second (third, fourth) manifest merges by id, as it always did;
* a cross-reference manifest — ``PATCHES.json``, whose rows carry their own
  ``bug_NN`` id and name the finding they fix — attaches as a labelled block,
  *not* merged, because "verdict: ACCEPT" there is a patch review and "verdict:
  true_positive" in TRIAGE.json is the panel, and collapsing them loses one;
* a rollup's per-finding sections are cut out and attached, and whatever is left
  (summary tables, cluster narrative, appendices) imports as one overview report
  instead of the whole blob;
* a ``composition.md``'s "bug_86 depends on bug_83" notes become structured
  ``depends_on`` links, because the fix agent needs to know a patch doesn't
  compile without its sibling before it bases a branch on thin air.

Measured on the real archive: 124 of 124 patches join, and 129 findings each pick
up their PATCHES.json row plus their VULN-FINDINGS.md, TRIAGE.md and
MAINTAINER-REPORT.md sections.

The archive also ranks its own findings and that ranking is the only thing that
makes a 129-report backlog approachable, so :func:`_priority` reads it off
(severity, panel verdict, confidence, CVSS overlay) and the caller creates rows
highest-first.

Everything here is opt-out-safe: an archive with no recognisable manifest yields
no records and ``zip_import`` falls back to file-per-report. Nothing guesses at
free prose — a rollup is only cut where the file itself puts the finding id at
the head of a heading, a bullet or a line, and a file with fewer than
``_MIN_ROLLUP_ANCHORS`` of those is left whole.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Keys under which scanners put their list of findings.
_FINDINGS_KEYS = ("findings", "results", "vulnerabilities", "issues")

#: Keys that identify a finding within its manifest, best first.
_ID_KEYS = ("id", "finding_id", "identifier", "key")

#: Keys a patch's sidecar uses to name the finding it fixes.
_PATCH_LINK_KEYS = ("finding", "finding_id", "id")

#: Keys a *cross-reference* row uses to name the finding it is about.
#:
#: ``id`` is deliberately absent, unlike :data:`_PATCH_LINK_KEYS`: a PATCHES.json
#: row's own ``id`` is ``bug_01``, a different namespace from ``f001``, and
#: accepting it would key the row under an id no finding has.
_XREF_LINK_KEYS = ("finding", "finding_id", "finding_ref")

#: Keys under which a cross-reference row carries its own scanner-local name, so
#: ``review_verdicts.txt``'s ``bug_94:`` lines can be resolved to ``f094``.
_ALIAS_KEYS = ("id", "bug", "patch", "name")

#: Cap on findings expanded from one archive. The same order of magnitude as
#: ``zip_import.MAX_ENTRIES`` and for the same reason: one manifest can claim any
#: number of findings, and each becomes a row plus a potential triage run.
MAX_FINDINGS = 2_000

#: Field order for the rendered body. Whatever the scanner also provides is
#: appended after these, so an unknown key is surfaced rather than dropped —
#: losing a field silently is how a real detail goes missing from a triage prompt.
_PREFERRED_ORDER = (
    "title",
    "category",
    "severity",
    "original_severity",
    "verdict",
    "confidence",
    "mean_conf",
    "cvss_score",
    "file",
    "line",
    "description",
    "exploit_scenario",
    "exploit_summary",
    "preconditions",
    "impact",
    "recommendation",
)

#: Manifest-level keys worth repeating on every finding: without them a finding
#: says "line 412 of Foo.java" with no indication of which repo or commit.
_CONTEXT_KEYS = ("target", "repo", "commit", "run", "scope", "branch", "generated", "date")

#: Entries worth cutting per-finding sections out of. Text only, and only names a
#: rollup plausibly has — a ``.json`` is either a manifest or it isn't, and
#: splitting one on line prefixes would be nonsense.
_ROLLUP_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".text"})

#: How many distinct findings a file must anchor before it's treated as a rollup.
#: Two mentions is a cross-reference in someone's prose; a dozen is a rollup. Set
#: low enough for a small archive and high enough that a note saying "see also
#: f012/f013" isn't torn in half.
_MIN_ROLLUP_ANCHORS = 3

#: Cap on one attached section. Real ones are 1-3 KB; this is here so a rollup
#: that anchors a finding once and then runs for 300 KB can't put all of it on one
#: report (and from there into a triage prompt).
MAX_ATTACHMENT_CHARS = 20_000

#: Cap on attachments per finding, for the same reason — an archive with fifty
#: rollups shouldn't produce a fifty-section report.
MAX_ATTACHMENTS = 12

#: Cap on the provenance document. It lands on every finding, so its size is
#: multiplied by the finding count and goes straight into every triage prompt.
MAX_PROVENANCE_CHARS = 4_000


@dataclass
class Attachment:
    """A block of per-finding text lifted out of some other entry."""

    #: Entry it came from, plus the alias it was filed under where that differs
    #: from the finding id — ``PATCHES.json (bug_01)``.
    label: str
    text: str


@dataclass
class FindingRecord:
    """One finding, ready to become a :class:`SecurityReport`."""

    finding_id: str
    title: str
    body: str
    #: Entry the finding was expanded from, for the operator and for provenance.
    origin: str
    patch: str = ""
    patch_path: str = ""
    #: The archive's own ranking of this finding, higher first. See :func:`_priority`.
    priority: float = 0.0
    #: What that number was read off, for the dashboard to show.
    priority_reason: str = ""
    #: Other finding ids this one's patch needs applied first, from the
    #: archive's composition notes. See :func:`_dependencies`.
    depends_on: list[str] = field(default_factory=list)

    @property
    def origin_label(self) -> str:
        """What to call this in the per-entry outcome list.

        The manifest path plus the finding id, because "VULN-FINDINGS.json" 129
        times tells the operator nothing about which one was skipped.
        """
        return f"{self.origin}#{self.finding_id}" if self.origin else self.finding_id


@dataclass
class Bundle:
    """The files a scanner shipped alongside one finding."""

    finding_id: str
    patch_path: str = ""
    patch: str = ""
    note_path: str = ""
    note: str = ""
    consumed: set[str] = field(default_factory=set)


@dataclass
class ScanArchive:
    """What the expander made of an archive."""

    findings: list[FindingRecord] = field(default_factory=list)
    #: Entries the expander consumed, which the generic walk must then skip or
    #: the same content imports twice — once split, once as a blob.
    consumed: set[str] = field(default_factory=set)
    #: Entries the expander only *partly* consumed: the text left over once the
    #: per-finding sections were lifted out. The generic walk imports this in
    #: place of the entry's real bytes, so a 343 KB rollup becomes a readable
    #: overview instead of vanishing (its cluster narrative and summary tables
    #: are not per-finding and are worth keeping) or importing whole (which is
    #: what made it unreadable).
    residuals: dict[str, str] = field(default_factory=dict)
    truncated: bool = False

    @property
    def recognised(self) -> bool:
        return bool(self.findings)


@dataclass
class _Index:
    """Everything one pass over the archive found out about it."""

    #: ``(entry name, manifest payload, findings list)`` per findings manifest.
    manifests: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = field(default_factory=list)
    bundles: dict[str, Bundle] = field(default_factory=dict)
    #: Finding id -> cross-reference rows about it, in entry-name order.
    xrefs: dict[str, list[Attachment]] = field(default_factory=dict)
    #: Scanner-local name (``bug_01``) -> finding id (``f001``).
    aliases: dict[str, str] = field(default_factory=dict)
    #: Entries already spoken for by a bundle, so a per-finding note isn't also
    #: run through the rollup splitter.
    claimed: set[str] = field(default_factory=set)
    #: What is left of a cross-reference manifest once its rows were attached.
    residuals: dict[str, str] = field(default_factory=dict)
    #: Cross-reference manifests with nothing left over.
    spent: set[str] = field(default_factory=set)


def expand_scan_archive(archive: zipfile.ZipFile, read_entry: Any) -> ScanArchive:
    """Find a findings manifest in *archive* and expand it, one record per finding.

    *read_entry* is a callable taking a :class:`zipfile.ZipInfo` and returning
    bytes or None, so the caller's size and codec limits still apply — this module
    never reads an entry itself.

    Returns an empty result for anything it doesn't recognise. Never raises: a
    malformed manifest is a reason to fall back to file-per-report, not to fail
    the import.
    """
    result = ScanArchive()
    try:
        index = _index(archive, read_entry)
    except Exception:
        logger.warning("Could not index scan archive; falling back", exc_info=True)
        return ScanArchive()

    if not index.manifests:
        return result

    # Merge every manifest by finding id, so TRIAGE.json's panel verdict lands on
    # the same record as VULN-FINDINGS.json's description instead of becoming a
    # second report about the same finding.
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    context: dict[str, Any] = {}
    for entry_name, payload, findings in index.manifests:
        result.consumed.add(entry_name)
        context.update(_context_of(payload))
        for raw in findings:
            fid = _finding_id(raw)
            if not fid:
                continue
            if fid not in merged:
                merged[fid] = {}
                order.append(fid)
            # First manifest wins per key: a later file adding "severity" is new
            # information, a later file *disagreeing* is not something this can
            # adjudicate, and silently overwriting would hide it.
            for key, value in raw.items():
                merged[fid].setdefault(key, value)

    result.residuals.update(index.residuals)
    result.consumed |= index.spent

    # Rollups last, because which entries are still up for grabs depends on what
    # the bundles claimed, and the anchors it matches depend on the finding ids
    # and aliases everything above established.
    #
    # Capped to the findings that will actually become records: cutting a section
    # out for a finding past MAX_FINDINGS would delete it from the rollup's
    # residual and then drop it with the finding, so the only copy in the archive
    # would go missing. Left uncut, it stays in the overview report.
    rollups = _split_rollups(archive, read_entry, set(order[:MAX_FINDINGS]), index, result)
    dependencies = _dependencies(archive, read_entry, set(order), index.aliases)

    # After the rollup split, deliberately. A PROVENANCE that turns out to anchor
    # findings is a rollup and gets cut up like one; taking it first pasted the
    # whole thing — every finding's section — onto every finding.
    provenance = _provenance(archive, read_entry, result)
    if provenance:
        context["provenance"] = provenance

    for fid in order[:MAX_FINDINGS]:
        fields = merged[fid]
        bundle = index.bundles.get(fid)
        attachments = [*index.xrefs.get(fid, ()), *rollups.get(fid, ())]
        if bundle is not None:
            result.consumed |= bundle.consumed
            if bundle.note.strip():
                # Appended, not substituted. The manifest has the structured fields
                # triage parses; the note is the prose a human wrote about the same
                # finding, and it was importing as a *separate* report — 97 of them in
                # one archive, each a near-duplicate of a finding already expanded.
                attachments.append(Attachment(label=bundle.note_path, text=bundle.note))
        priority, reason = _priority(fields, has_patch=bool(bundle and bundle.patch))
        result.findings.append(
            FindingRecord(
                finding_id=fid,
                title=_title_for(fid, fields),
                body=_body(fid, fields, context, attachments),
                origin=_origin_of(fid, index.manifests),
                patch=bundle.patch if bundle else "",
                patch_path=bundle.patch_path if bundle else "",
                priority=priority,
                priority_reason=reason,
                depends_on=dependencies.get(fid, []),
            )
        )
    if len(order) > MAX_FINDINGS:
        result.truncated = True
        logger.warning(
            "Scan archive claims %d findings; expanded the first %d", len(order), MAX_FINDINGS
        )
    return result


def _index(archive: zipfile.ZipFile, read_entry: Any) -> _Index:
    """Locate findings manifests, then everything else that refers to them.

    Three things come out of the JSON pass: findings manifests, patch sidecars,
    and cross-reference rows. The last two can't be resolved until the first has
    produced a set of known ids — a patch directory identifies its finding either
    in a JSON sidecar (``{"finding": "f001"}``) or, in the two archives that ship
    prose instead, in the heading of a sibling note (``# bug_80 (f080) — …``), and
    matching against *known* ids rather than a guessed ``f\\d+`` pattern means the
    link is only made when the manifest actually has that finding.
    """
    index = _Index()
    sidecars: dict[str, dict[str, Any]] = {}
    #: Deferred: ``(entry name, payload, [(key, rows), …])`` for every list of
    #: dicts that might be about findings we haven't enumerated yet.
    candidates: list[tuple[str, dict[str, Any], list[tuple[str, list[dict[str, Any]]]]]] = []

    for info in archive.infolist():
        if info.is_dir() or not info.filename.lower().endswith(".json"):
            continue
        raw = read_entry(info)
        if raw is None:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, list):
            # A bare list of findings is a manifest too.
            if payload and all(isinstance(v, dict) for v in payload):
                index.manifests.append((info.filename, {}, payload))
            continue
        if not isinstance(payload, dict):
            continue
        findings = _findings_in(payload)
        if findings is not None:
            index.manifests.append((info.filename, payload, findings))
            continue
        if any(k in payload for k in _PATCH_LINK_KEYS):
            sidecars[info.filename] = payload
        linked = _linked_lists(payload)
        if linked:
            candidates.append((info.filename, payload, linked))

    known = {fid for _n, _p, fs in index.manifests for fid in map(_finding_id, fs) if fid}
    index.bundles = _bundles(archive, read_entry, sidecars, known)
    for bundle in index.bundles.values():
        index.claimed |= bundle.consumed
    _collect_xrefs(candidates, known, index)
    _alias_bundles(index)
    return index


def _linked_lists(payload: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Lists of dicts in *payload* that look like they're about findings.

    Any value that is a non-empty list of dicts where at least one row names a
    finding. ``PATCHES.json`` puts its 124 rows under ``"patches"``, which is not
    a key worth adding to :data:`_FINDINGS_KEYS` — those rows are *about* findings
    rather than being them, and merging them in would have collided ``title``,
    ``severity`` and ``verdict`` with the panel's own. Keying off the link instead
    of the key name means the next scanner's ``"fixes"`` works without an edit.
    """
    found = []
    for key, value in payload.items():
        if not isinstance(value, list) or not value:
            continue
        rows = [v for v in value if isinstance(v, dict)]
        if len(rows) == len(value) and any(_xref_link(r) for r in rows):
            found.append((key, rows))
    return found


def _xref_link(row: dict[str, Any]) -> str:
    for key in _XREF_LINK_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)[:100]
    return ""


def _collect_xrefs(
    candidates: list[tuple[str, dict[str, Any], list[tuple[str, list[dict[str, Any]]]]]],
    known: set[str],
    index: _Index,
) -> None:
    """File each cross-reference row under the finding it names.

    Rows pointing at an id no manifest declared are dropped rather than kept under
    a made-up finding, and an entry is only claimed once at least one row landed —
    otherwise a file full of unrelated dict lists would vanish.

    What's left of the payload once the linked lists are lifted out is a
    residual, same as for a markdown rollup: ``PATCHES.json``'s 296 KB is 124
    per-finding rows plus a run summary, and that summary is worth a small report
    even though the rows aren't worth a second copy of themselves.
    """
    for entry_name, payload, linked in sorted(candidates, key=lambda c: c[0]):
        spent_keys: set[str] = set()
        for key, rows in linked:
            for row in rows:
                fid = _xref_link(row)
                if fid not in known:
                    continue
                alias = _alias_of(row)
                label = f"{entry_name} ({alias})" if alias and alias != fid else entry_name
                text = _render_fields(row, skip={*_XREF_LINK_KEYS})
                if not text:
                    continue
                index.xrefs.setdefault(fid, []).append(Attachment(label=label, text=text))
                if alias:
                    index.aliases.setdefault(alias, fid)
                spent_keys.add(key)
        if not spent_keys:
            continue
        index.claimed.add(entry_name)
        leftover = _render_fields(
            {k: v for k, v in payload.items() if k not in spent_keys}, skip=set()
        )
        if leftover:
            # Headed, because a bare wall of labelled lines gives the operator no
            # way to tell a run summary from a report — and because the entry has
            # to clear zip_import's security-keyword gate on its own merits now
            # that the findings that used to carry it have moved out.
            index.residuals[entry_name] = (
                f"Security scan artifact: {entry_name} (run-level summary)\n"
                f"Its per-finding records were attached to the matching findings.\n\n"
                f"{leftover}"
            )
        else:
            index.spent.add(entry_name)


def _alias_of(row: dict[str, Any]) -> str:
    for key in _ALIAS_KEYS:
        value = row.get(key)
        if isinstance(value, str | int) and str(value).strip():
            return str(value).strip()[:100]
    return ""


def _alias_bundles(index: _Index) -> None:
    """Alias each bundle directory's name to the finding it holds.

    ``PATCHES/bug_03/`` holding f003 is what lets ``review_verdicts.txt``'s
    ``bug_03: ACCEPT`` line find its finding in an archive that ships no
    PATCHES.json to say so.
    """
    for fid, bundle in index.bundles.items():
        for name in (bundle.patch_path, bundle.note_path):
            if "/" not in name:
                continue
            parent = name.rsplit("/", 1)[0].rsplit("/", 1)[-1]
            if parent:
                index.aliases.setdefault(parent, fid)


def _bundles(
    archive: zipfile.ZipFile,
    read_entry: Any,
    sidecars: dict[str, dict[str, Any]],
    known: set[str],
) -> dict[str, Bundle]:
    """Group patches and notes by the finding they belong to, one dir at a time."""
    by_dir: dict[str, list[zipfile.ZipInfo]] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        parent = info.filename.rsplit("/", 1)[0] if "/" in info.filename else ""
        by_dir.setdefault(parent, []).append(info)

    bundles: dict[str, Bundle] = {}
    for parent, infos in by_dir.items():
        patches = sorted((i for i in infos if _is_patch(i.filename)), key=lambda i: i.filename)
        if not patches:
            continue
        notes = [i for i in infos if _is_note(i.filename)]
        sidecar_here = [
            n for n in sidecars if n.rsplit("/", 1)[0] == parent or (not parent and "/" not in n)
        ]

        fid = ""
        for name in sidecar_here:
            fid = next(
                (str(sidecars[name][k]) for k in _PATCH_LINK_KEYS if sidecars[name].get(k)), ""
            )
            if fid:
                break

        note_text, note_path = "", ""
        for info in sorted(notes, key=lambda i: i.filename):
            raw = read_entry(info)
            if raw is None:
                continue
            text = raw.decode("utf-8", errors="replace")
            if not note_text:
                note_text, note_path = text, info.filename
            if not fid:
                # Only the head: an id mentioned in passing halfway down a note is
                # a cross-reference, not this bundle's subject.
                fid = _id_in(text[:2048], known)

        if not fid:
            continue
        if fid in bundles:
            # Two directories claiming one finding. Says so rather than dropping
            # silently — this is how the _id_in bug showed itself.
            #
            # The loser's files still count as consumed. Returning here without
            # recording them let its note flow into the generic walk and import as
            # a separate near-duplicate report — the double-import `consumed`
            # exists to prevent, and "keeping the first" would have been only half
            # true: the second's prose was kept too, as its own row.
            bundles[fid].consumed |= (
                {i.filename for i in patches} | {i.filename for i in notes} | set(sidecar_here)
            )
            logger.warning(
                "Scan archive: %s and %s both resolve to finding %s; keeping the first",
                bundles[fid].patch_path,
                primary_name(patches),
                fid,
            )
            continue

        # The primary patch is the plainest name; revisions (patch.diff.r2,
        # patch.diff.pre-stack) are kept as extras so they're neither lost nor
        # mistaken for separate findings.
        primary = patches[0]
        raw = read_entry(primary)
        if raw is None:
            continue
        bundles[fid] = Bundle(
            finding_id=fid,
            patch_path=primary.filename,
            patch=raw.decode("utf-8", errors="replace"),
            note_path=note_path,
            note=note_text,
            consumed={i.filename for i in patches}
            | {i.filename for i in notes}
            | set(sidecar_here),
        )
    return bundles


def primary_name(patches: list[zipfile.ZipInfo]) -> str:
    return patches[0].filename if patches else "(none)"


def _is_patch(name: str) -> bool:
    """Whether *name* is a patch, including a revision of one.

    ``patch.diff.r2`` and ``patch.diff.pre-stack`` are real entries in a real
    archive, and a suffix-only test called them ".r2" and ".pre-stack" — unknown
    extensions, so they imported as text *reports* whose body is a diff. 19 of
    them in one archive.
    """
    base = name.rsplit("/", 1)[-1].lower()
    return ".diff" in base or ".patch" in base


def _is_note(name: str) -> bool:
    base = name.rsplit("/", 1)[-1].lower()
    return base in ("notes.md", "notes.txt", "readme.md", "report.md", "description.md")


def _id_in(text: str, known: set[str]) -> str:
    """The known finding id appearing *earliest* in *text*.

    Earliest, not "first one I happen to iterate": these notes are headed
    ``# bug_02 — f002: ...`` and then cross-reference sibling findings in the
    body, so position is the signal. Sorting the candidate ids by length and
    taking the first match looked equivalent and wasn't — every id in a real
    archive is the same length, so that reduced to set iteration order and 16 of
    97 bundles latched onto a cross-referenced id, collided with the bundle that
    genuinely owned it, and were dropped.

    Ties at the same offset go to the longer id, so "f10" can't win over "f100".
    """
    best_at, best_id = len(text) + 1, ""
    for fid in known:
        at = text.find(fid)
        if at == -1:
            continue
        if at < best_at or (at == best_at and len(fid) > len(best_id)):
            best_at, best_id = at, fid
    return best_id


def _findings_in(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The findings list in *payload*, or None if this isn't a manifest.

    Requires a *list of dicts*: a ``"findings": 129`` count or a list of strings
    is not something to expand, and treating it as one would produce a pile of
    empty reports.
    """
    for key in _FINDINGS_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            return value
    return None


def _finding_id(raw: dict[str, Any]) -> str:
    for key in _ID_KEYS:
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)[:100]
    return ""


def _provenance(archive: zipfile.ZipFile, read_entry: Any, result: ScanArchive) -> str:
    """The archive's PROVENANCE document, to de-normalise onto every finding.

    It is the same class of thing as ``_CONTEXT_KEYS`` — which repo, which branch,
    which pinned commit — for archives that record it in a file rather than in a
    manifest field. And it is the whole answer to "3.5 of what, exactly", which
    matters most for the branch a finding is mapped against.

    It usually has no security keywords of its own, so the generic walk was
    rejecting it as "not-a-report" and dropping the only copy of which commit was
    scanned. Consumed here instead — except when it is too long to attach whole,
    where the full text is left as a residual so nothing goes missing.
    """
    # Shallowest path first: a scanner ships PATCHES/bug_01/provenance.txt about
    # one patch, and plain name-sort put that ahead of the archive's own
    # PROVENANCE.md — the top-level document, which is the one that names the
    # branch, then never got attached at all.
    candidates = sorted(
        (
            info
            for info in archive.infolist()
            if not info.is_dir()
            and _suffix(info.filename) in _ROLLUP_SUFFIXES
            and info.filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() == "provenance"
        ),
        key=lambda i: (i.filename.count("/"), i.filename),
    )
    for info in candidates:
        if info.filename in result.consumed or info.filename in result.residuals:
            # The rollup splitter already claimed it, so it anchors findings and is
            # a rollup rather than run-level context. Its sections are attached.
            continue
        raw = read_entry(info)
        if raw is None:
            continue
        text: str = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        if len(text) > MAX_PROVENANCE_CHARS:
            # Attached truncated, kept whole. Silently dropping the rest would lose
            # bytes the operator had before this function existed, and with no
            # per-entry outcome to notice it by.
            result.residuals[info.filename] = text
            logger.info(
                "Scan archive: %s is %d chars; attaching the first %d as scan context "
                "and importing the whole thing as its own report.",
                info.filename,
                len(text),
                MAX_PROVENANCE_CHARS,
            )
            return f"{text[:MAX_PROVENANCE_CHARS]}\n(truncated — full text imported separately)"
        result.consumed.add(info.filename)
        logger.info("Scan archive: %s attached to every finding as scan context", info.filename)
        return text
    return ""


def _context_of(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: payload[k] for k in _CONTEXT_KEYS if payload.get(k) not in (None, "", [], {})}


def _origin_of(fid: str, manifests: list[tuple[str, dict[str, Any], list[dict[str, Any]]]]) -> str:
    for name, _payload, findings in manifests:
        if any(_finding_id(f) == fid for f in findings):
            return name
    return ""


def _title_for(fid: str, fields: dict[str, Any]) -> str:
    """``f001: Client identity bound from first unauthenticated frame``.

    The id leads because it's what the archive's other files refer to, and it's
    what makes 129 sibling findings distinguishable in a list.
    """
    for key in ("title", "summary", "name", "category"):
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            return f"{fid}: {value.strip()}"[:500]
    where = fields.get("file")
    if isinstance(where, str) and where.strip():
        return f"{fid}: finding in {where.strip()}"[:500]
    return f"Finding {fid}"[:500]


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

#: Severity tiers, in the spellings scanners actually use. The gap between tiers
#: is wide enough that no confidence or CVSS bonus below can lift a LOW over a
#: MEDIUM — severity is the operator's first question and stays the coarse sort.
_SEVERITY_BASE = {
    "critical": 100.0,
    "high": 80.0,
    "medium": 50.0,
    "moderate": 50.0,
    "low": 20.0,
    "informational": 5.0,
    "info": 5.0,
    "none": 5.0,
}

#: Above LOW and below MEDIUM: an unranked finding should not outrank one the
#: panel called MEDIUM, nor sink below one it called LOW.
_UNRANKED_BASE = 35.0

#: What a triage panel's verdict does to the tier. A refuted finding keeps a
#: nonzero score so it still sorts *among* the false positives rather than
#: collapsing into a tie with every other one.
_VERDICT_WEIGHT = {
    "true_positive": 1.0,
    "true-positive": 1.0,
    "truepositive": 1.0,
    "tp": 1.0,
    "confirmed": 1.0,
    "valid": 1.0,
    "needs_manual_test": 0.6,
    "needs-manual-test": 0.6,
    "needs_manual": 0.6,
    "unclear": 0.6,
    "undetermined": 0.6,
    "duplicate": 0.3,
    "false_positive": 0.05,
    "false-positive": 0.05,
    "falsepositive": 0.05,
    "fp": 0.05,
    "invalid": 0.05,
    "refuted": 0.05,
}


def _priority(fields: dict[str, Any], *, has_patch: bool) -> tuple[float, str]:
    """Rank one finding from what the archive already decided about it.

    A 129-finding import is unusable in arrival order — the archive's own two
    HIGHs were at positions 3 and 94 — and the scanner has already done the work:
    a severity tier, a panel verdict, a confidence, and sometimes a CVSS overlay.
    This reads those off rather than asking an LLM, so the ordering costs nothing
    and exists before triage rather than after it.

    Returns ``(score, reason)``; the reason is what the dashboard shows, because a
    bare number nobody can account for is a number nobody trusts.
    """
    parts: list[str] = []

    severity = _text(fields.get("severity")) or _text(fields.get("original_severity"))
    base = _SEVERITY_BASE.get(severity.lower(), _UNRANKED_BASE) if severity else _UNRANKED_BASE
    if severity:
        parts.append(severity.upper())

    verdict = _text(fields.get("verdict")) or _text(fields.get("triage_verdict"))
    weight = _VERDICT_WEIGHT.get(verdict.lower().replace(" ", "_"), 1.0) if verdict else 1.0
    if verdict:
        parts.append(verdict.lower())
    score = base * weight

    confidence = max(
        _fraction(fields.get("confidence")),
        _fraction(fields.get("mean_conf")),
        _fraction(fields.get("panel_confidence")),
    )
    if confidence:
        score += 20.0 * confidence
        parts.append(f"conf {confidence:.2f}")

    cvss = _clamp(_number(fields.get("cvss_score") or fields.get("cvss")), 0.0, 10.0)
    if cvss:
        score += cvss
        parts.append(f"CVSS {cvss:g}")

    if fields.get("critical_candidate") is True:
        score += 15.0
        parts.append("critical candidate")

    # Replication is weak evidence and priced as such: four researchers finding
    # the same thing independently is worth a nudge, not a tier.
    discoveries = int(_clamp(_number(fields.get("independent_discoveries")), 0.0, 5.0))
    if discoveries > 1:
        score += 2.0 * discoveries
        parts.append(f"{discoveries} discoveries")

    if has_patch:
        # Not a claim about severity — a claim about cost. A finding that arrives
        # with a diff is cheaper to act on than one that doesn't, so among equals
        # it goes first.
        score += 3.0
        parts.append("patch")

    return round(score, 3), ", ".join(parts)[:200]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def _fraction(value: Any) -> float:
    """A confidence as 0..1, whichever scale the scanner used.

    Both spellings are in the wild — ``0.79`` and ``79`` — and reading a
    percentage as a fraction would have made every such finding look
    maximally confident.
    """
    number = _number(value)
    if number > 1.0:
        number /= 100.0
    return _clamp(number, 0.0, 1.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# Rollup splitting
# --------------------------------------------------------------------------- #

#: A line that puts an id at its head. The optional marker is a heading (``###``)
#: or a list bullet (``-``), then up to three emphasis characters, then the token.
#:
#: Table pipes are deliberately not in the marker set: ``| f003 | 7.5 | …`` is a
#: row of a per-finding table, and claiming it would cut the table apart and
#: leave the residual's header rows dangling over nothing.
_ANCHOR_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?:(?P<heading>\#{1,6})|(?P<bullet>[-*+]))?[ \t]*"
    r"[*_`]{0,3}"
    r"(?P<token>[A-Za-z][A-Za-z0-9_.-]{0,63})"
)

_HEADING_RE = re.compile(r"^ {0,3}(\#{1,6})[ \t]")

_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")


@dataclass
class _Anchor:
    line: int
    finding_id: str
    #: Heading depth, or 0 for a bullet or bare line.
    level: int
    indent: int


def _split_rollups(
    archive: zipfile.ZipFile,
    read_entry: Any,
    known: set[str],
    index: _Index,
    result: ScanArchive,
) -> dict[str, list[Attachment]]:
    """Cut per-finding sections out of every rollup, recording what's left over.

    Populates ``result.residuals`` for each file it cut, so the generic walk
    imports the remainder — the summary tables and cluster narrative that are
    genuinely about the run rather than about one finding.
    """
    sections: dict[str, list[Attachment]] = {}
    for info in sorted(archive.infolist(), key=lambda i: i.filename):
        if info.is_dir() or _suffix(info.filename) not in _ROLLUP_SUFFIXES:
            continue
        if info.filename in index.claimed or info.filename in result.consumed:
            continue
        raw = read_entry(info)
        if raw is None:
            continue
        text = raw.decode("utf-8", errors="replace")
        cut, residual = _split_rollup(text, known, index.aliases)
        if not cut:
            continue
        for fid, body in cut.items():
            sections.setdefault(fid, []).append(Attachment(label=info.filename, text=body))
        if residual:
            # Says where the rest went. Without it the operator opens what looks
            # like a truncated TRIAGE.md and has no way to know its f003 section
            # is now on the f003 report.
            result.residuals[info.filename] = (
                f"{residual}\n\n"
                f"-- {len(cut)} per-finding section(s) from this file were attached "
                f"to the matching findings --"
            )
        else:
            # Nothing but per-finding sections. Consumed outright rather than
            # imported as a report holding only that footer.
            result.consumed.add(info.filename)
        logger.info(
            "Scan archive: lifted %d per-finding section(s) out of %s", len(cut), info.filename
        )
    return sections


def _split_rollup(
    text: str, known: set[str], aliases: dict[str, str]
) -> tuple[dict[str, str], str]:
    """Split *text* into per-finding sections and everything else.

    Returns ``({finding id: section}, residual)``, or ``({}, "")`` when the file
    doesn't anchor enough distinct findings to be a rollup — in which case the
    caller leaves it alone entirely.
    """
    lines = text.splitlines()
    anchors = _anchors(lines, known, aliases)
    if len({a.finding_id for a in anchors}) < _MIN_ROLLUP_ANCHORS:
        return {}, ""

    anchor_lines = {a.line for a in anchors}
    claimed: set[int] = set()
    sections: dict[str, list[str]] = {}
    for anchor in anchors:
        end = _section_end(lines, anchor, anchor_lines)
        claimed.update(range(anchor.line, end))
        sections.setdefault(anchor.finding_id, []).append("\n".join(lines[anchor.line : end]))

    cut = {
        fid: _cap("\n\n".join(block.strip() for block in blocks).strip())
        for fid, blocks in sections.items()
    }
    residual = _tidy("\n".join(line for n, line in enumerate(lines) if n not in claimed))
    return {fid: body for fid, body in cut.items() if body}, residual


def _outside_fences(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(index, line)`` for the lines outside fenced code blocks.

    ``PATCHES.md`` and ``composition.md`` both quote shell transcripts, and a
    ``bug_01`` inside one is an example, not a declaration — every scan of these
    files skips fences the same way.
    """
    fence = ""
    for number, line in enumerate(lines):
        opening = _FENCE_RE.match(line)
        if opening:
            marker = opening.group(1)
            fence = "" if fence == marker else (fence or marker)
            continue
        if fence:
            continue
        yield number, line


def _anchors(lines: list[str], known: set[str], aliases: dict[str, str]) -> list[_Anchor]:
    """Every line whose leading token names a known finding, outside code fences."""
    anchors: list[_Anchor] = []
    for number, line in _outside_fences(lines):
        match = _ANCHOR_RE.match(line)
        if match is None:
            continue
        token = match.group("token")
        fid = token if token in known else aliases.get(token, "")
        if not fid or fid not in known:
            continue
        heading = match.group("heading")
        anchors.append(
            _Anchor(
                line=number,
                finding_id=fid,
                level=len(heading) if heading else 0,
                indent=len(match.group("indent").expandtabs(4)),
            )
        )
    return anchors


#: "bug_86 depends on bug_83's conf" — the shape composition notes declare
#: inter-finding build dependencies in. Both tokens still have to resolve to a
#: known finding before anyone believes them; the regex just finds candidates.
#: One pair per match: "bug_1 depends on bug_2 and bug_3" only links bug_2 —
#: write one line per dependency instead.
_DEPENDS_RE = re.compile(
    r"\b(?P<src>[A-Za-z][\w-]*?)\s+depends\s+on\s+(?P<dst>[A-Za-z][\w-]*?)\b",
    re.IGNORECASE,
)


def _dependencies(
    archive: zipfile.ZipFile,
    read_entry: Any,
    known: set[str],
    aliases: dict[str, str],
) -> dict[str, list[str]]:
    """Inter-finding build dependencies declared in composition notes.

    Archives that ship patches sometimes ship a ``composition.md`` saying which
    patches need which others ("bug_86 depends on bug_83's conf — cherry-picking
    one without the other does not compile"). That is exactly what the fix agent
    needs to know before it bases a branch, so it becomes structure rather than
    staying prose. A pair only counts when *both* tokens resolve to a finding
    the manifest actually has — "this depends on Spark 3.5" is not a link.
    """
    by_lower = {token.lower(): fid for token, fid in aliases.items()}

    def resolve(token: str) -> str:
        if token in known:
            return token
        return aliases.get(token) or by_lower.get(token.lower(), "")

    pairs: dict[str, list[str]] = {}
    for info in archive.infolist():
        if info.is_dir() or info.filename.rsplit("/", 1)[-1].lower() != "composition.md":
            continue
        raw = read_entry(info)
        if raw is None:
            continue
        for _, line in _outside_fences(raw.decode("utf-8", errors="replace").splitlines()):
            for match in _DEPENDS_RE.finditer(line):
                src = resolve(match.group("src"))
                dst = resolve(match.group("dst"))
                if not src or not dst or src == dst:
                    continue
                bucket = pairs.setdefault(src, [])
                if dst not in bucket:
                    bucket.append(dst)
    return pairs


def _section_end(lines: list[str], anchor: _Anchor, anchor_lines: set[int]) -> int:
    """Where *anchor*'s section stops.

    A heading section runs to the next anchor or the next heading at the same or
    shallower depth — so ``### f003`` ends at ``### f013`` or at ``## True
    positives — MEDIUM``, but swallows a ``#### `` of its own.

    A bullet or bare-line section runs while the following lines are blank or
    more deeply indented, which is exactly how ``MAINTAINER-REPORT.md`` writes a
    finding (a ``- **f003 …**`` bullet with indented ``Panel:`` / ``What goes
    wrong:`` continuation) and how ``review_verdicts.txt`` writes one (a single
    ``bug_94: ACCEPT | …`` line).
    """
    for number in range(anchor.line + 1, len(lines)):
        if number in anchor_lines:
            return number
        line = lines[number]
        heading = _HEADING_RE.match(line)
        if anchor.level:
            if heading and len(heading.group(1)) <= anchor.level:
                return number
            continue
        if not line.strip():
            continue
        # expandtabs on both sides, as _anchors does when it records the anchor's
        # own indent: comparing a tab-indented anchor against a space-indented
        # continuation by raw character count made a continuation look shallower
        # than the bullet it belongs to and ended the section a line early.
        if len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip()) <= anchor.indent:
            return number
    return len(lines)


def _cap(text: str) -> str:
    if len(text) <= MAX_ATTACHMENT_CHARS:
        return text
    return text[:MAX_ATTACHMENT_CHARS].rstrip() + "\n… (section truncated)"


def _tidy(text: str) -> str:
    """Collapse the blank-line runs that cutting sections out leaves behind."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _body(
    fid: str, fields: dict[str, Any], context: dict[str, Any], attachments: list[Attachment]
) -> str:
    """The finding's manifest fields, then everything else that was about it.

    Attachments are appended in a fixed order (cross-references by entry name,
    then rollup sections by entry name, then the bundle note) because the body is
    the dedup key: a re-import has to render byte-identically or the archive
    imports twice.
    """
    blocks = [_render(fid, fields, context)]
    for attachment in attachments[:MAX_ATTACHMENTS]:
        text = attachment.text.strip()
        if text:
            blocks.append(f"-- {attachment.label} --\n{text}")
    if len(attachments) > MAX_ATTACHMENTS:
        blocks.append(f"({len(attachments) - MAX_ATTACHMENTS} further section(s) not attached)")
    return "\n\n".join(blocks)


def _render(fid: str, fields: dict[str, Any], context: dict[str, Any]) -> str:
    """Render a finding as the text a human (and the triage prompt) reads.

    Prose, not the raw JSON: the triage parser is prompted to pull component,
    POC and impact out of a report, and it does that better from labelled lines
    than from a nested object. Unknown keys are appended rather than dropped.
    """
    body = f"Finding: {fid}"
    fields_text = _render_fields(fields, skip={"id", *_ID_KEYS})
    if fields_text:
        body = f"{body}\n{fields_text}"
    if context:
        body = f"{body}\n\n-- scan context --\n{_render_fields(context, skip=set())}"
    return body


def _render_fields(fields: dict[str, Any], skip: set[str]) -> str:
    """Labelled lines for *fields*, preferred keys first and the rest sorted.

    Empty values are dropped — ``_field_line`` returns "" for a blank one — so a
    scanner that ships every key with a null doesn't produce a wall of labels.
    """
    lines: list[str] = []
    seen = set(skip)
    for key in _PREFERRED_ORDER:
        if key in fields and key not in seen:
            seen.add(key)
            lines.append(_field_line(key, fields[key]))
    for key in sorted(fields):
        if key not in seen:
            lines.append(_field_line(key, fields[key]))
    return "\n".join(line for line in lines if line).strip()


def _field_line(key: str, value: Any) -> str:
    label = key.replace("_", " ").capitalize()
    if isinstance(value, list | tuple):
        rendered = ", ".join(str(v) for v in value)
    elif isinstance(value, dict):
        rendered = json.dumps(value, sort_keys=True)
    else:
        rendered = str(value)
    rendered = rendered.strip()
    if not rendered:
        return ""
    # Multi-line values get their own block so a long description doesn't run off
    # the side of a label.
    if "\n" in rendered:
        return f"\n{label}:\n{rendered}\n"
    return f"{label}: {rendered}"


def _suffix(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[-1].lower()
