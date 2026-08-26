"""Expand a scanner archive into one report per finding.

``zip_import`` treats an archive as a bag of files and makes one report per file.
That is right for a folder of forwarded emails and wrong for the output of an
automated scanner, which is the shape a maintainer actually gets handed. A real
one looked like this:

    VULN-FINDINGS.json      {"findings": [129 dicts], "target": ..., "commit": ...}
    TRIAGE.json             {"findings": [129 dicts]}   # a panel's take, same ids
    PATCHES/bug_01/meta.json  {"finding": "f001", "title": ..., "severity": ...}
    PATCHES/bug_01/patch.diff  the proposed fix
    ...                     124 more of those
    MAINTAINER-REPORT.md    a 75 KB prose rollup over the same 129 findings

File-per-report gave 107 reports out of that, of which the useful ones were a
284 KB blob nobody can triage and 124 patches imported as "reports" whose body is
a diff. What the operator wants is 129 findings, each with the patch that fixes
it — which the archive can express, because ``meta.json`` names the finding its
patch belongs to. Measured on the real thing: 124 of 124 patches join.

Everything here is opt-out-safe: an archive with no recognisable manifest yields
no records and ``zip_import`` falls back to file-per-report. Nothing guesses at
prose — splitting ``MAINTAINER-REPORT.md`` on headings would be inventing
structure, so the rollups are left to the generic path.
"""

from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Keys under which scanners put their list of findings.
_FINDINGS_KEYS = ("findings", "results", "vulnerabilities", "issues")

#: Keys that identify a finding within its manifest, best first.
_ID_KEYS = ("id", "finding_id", "identifier", "key")

#: Keys a patch's sidecar uses to name the finding it fixes.
_PATCH_LINK_KEYS = ("finding", "finding_id", "id")

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
    "confidence",
    "mean_conf",
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
    truncated: bool = False

    @property
    def recognised(self) -> bool:
        return bool(self.findings)


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
        manifests, bundles = _index(archive, read_entry)
    except Exception:
        logger.warning("Could not index scan archive; falling back", exc_info=True)
        return ScanArchive()

    if not manifests:
        return result

    # Merge every manifest by finding id, so TRIAGE.json's panel verdict lands on
    # the same record as VULN-FINDINGS.json's description instead of becoming a
    # second report about the same finding.
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    context: dict[str, Any] = {}
    for entry_name, payload, findings in manifests:
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

    for fid in order[:MAX_FINDINGS]:
        fields = merged[fid]
        bundle = bundles.get(fid)
        body = _render(fid, fields, context)
        if bundle is not None and bundle.note.strip():
            # Appended, not substituted. The manifest has the structured fields
            # triage parses; the note is the prose a human wrote about the same
            # finding, and it was importing as a *separate* report — 97 of them in
            # one archive, each a near-duplicate of a finding already expanded.
            body = f"{body}\n\n-- {bundle.note_path} --\n{bundle.note.strip()}"
            result.consumed |= bundle.consumed
        elif bundle is not None:
            result.consumed |= bundle.consumed
        result.findings.append(
            FindingRecord(
                finding_id=fid,
                title=_title_for(fid, fields),
                body=body,
                origin=_origin_of(fid, manifests),
                patch=bundle.patch if bundle else "",
                patch_path=bundle.patch_path if bundle else "",
            )
        )
    if len(order) > MAX_FINDINGS:
        result.truncated = True
        logger.warning(
            "Scan archive claims %d findings; expanded the first %d", len(order), MAX_FINDINGS
        )
    return result


def _index(
    archive: zipfile.ZipFile, read_entry: Any
) -> tuple[list[tuple[str, dict[str, Any], list[dict[str, Any]]]], dict[str, Bundle]]:
    """Locate findings manifests, then the per-finding bundle each one refers to.

    Two passes, because the second needs the first's finding ids: a patch
    directory identifies its finding either in a JSON sidecar (``{"finding":
    "f001"}``) or, in the two archives that ship prose instead, in the heading of
    a sibling note — ``# bug_80 (f080) — ...``. Matching against *known* ids
    rather than a guessed ``f\\d+`` pattern means the link is only made when the
    manifest actually has that finding.
    """
    manifests: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    sidecars: dict[str, dict[str, Any]] = {}

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
                manifests.append((info.filename, {}, payload))
            continue
        if not isinstance(payload, dict):
            continue
        findings = _findings_in(payload)
        if findings is not None:
            manifests.append((info.filename, payload, findings))
        elif any(k in payload for k in _PATCH_LINK_KEYS):
            sidecars[info.filename] = payload

    known = {fid for _n, _p, fs in manifests for fid in map(_finding_id, fs) if fid}
    return manifests, _bundles(archive, read_entry, sidecars, known)


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


def _render(fid: str, fields: dict[str, Any], context: dict[str, Any]) -> str:
    """Render a finding as the text a human (and the triage prompt) reads.

    Prose, not the raw JSON: the triage parser is prompted to pull component,
    POC and impact out of a report, and it does that better from labelled lines
    than from a nested object. Unknown keys are appended rather than dropped.
    """
    lines = [f"Finding: {fid}"]
    seen = {"id", *(_ID_KEYS)}
    for key in _PREFERRED_ORDER:
        if key in fields:
            seen.add(key)
            lines.append(_field_line(key, fields[key]))
    for key in sorted(fields):
        if key not in seen:
            lines.append(_field_line(key, fields[key]))
    if context:
        lines.append("-- scan context --")
        lines.extend(_field_line(k, v) for k, v in sorted(context.items()))
    # Empty strings dropped — _field_line returns one for a field whose value is
    # blank — and then the section break is re-inserted. Appending a bare "" as the
    # separator didn't work: this filter removed it, so "-- scan context --" ran
    # straight on from the last field and read as another value.
    body = "\n".join(line for line in lines if line)
    return body.replace("\n-- scan context --\n", "\n\n-- scan context --\n")


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
