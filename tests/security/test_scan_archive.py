"""Tests for expanding a scanner archive into one report per finding.

Shaped after the real archives that prompted this: a findings manifest, a panel's
second manifest over the same ids, and one directory per finding holding a patch
plus either a JSON sidecar or a prose note.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from unittest.mock import patch

import pytest

from franktheunicorn.core.models import SecurityReport
from franktheunicorn.security.scan_archive import expand_scan_archive
from franktheunicorn.security.zip_import import _read_entry, import_reports_from_zip

FINDINGS = {
    "target": "apache/spark",
    "commit": "abc123",
    "findings": [
        {
            "id": "f001",
            "category": "auth",
            "file": "core/Foo.java",
            "line": 412,
            "description": "Client identity is bound from the first unauthenticated frame.",
            "exploit_scenario": "A second client claims the first client's appId.",
            "confidence": 0.8,
        },
        {
            "id": "f002",
            "category": "path-traversal",
            "file": "core/Bar.java",
            "line": 88,
            "description": "mergeDir is not validated and escapes the local dir.",
        },
    ],
}

TRIAGE = {
    "findings": [
        {"id": "f001", "original_severity": "HIGH", "owner": "core"},
        {"id": "f002", "original_severity": "MEDIUM", "owner": "shuffle"},
    ]
}

PATCH_1 = "--- a/core/Foo.java\n+++ b/core/Foo.java\n@@ -1 +1 @@\n-bad\n+good\n"
PATCH_2 = "--- a/core/Bar.java\n+++ b/core/Bar.java\n@@ -1 +1 @@\n-worse\n+better\n"


def make_zip(entries: dict[str, bytes | str]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content.encode() if isinstance(content, str) else content)
    buf.seek(0)
    return buf


def expand(entries: dict[str, bytes | str]) -> Any:
    archive = zipfile.ZipFile(make_zip(entries))
    return expand_scan_archive(archive, lambda info: _read_entry(archive, info)[0])


def sidecar_archive() -> dict[str, bytes | str]:
    """The meta.json shape: a sidecar naming the finding its patch fixes."""
    return {
        "VULN-FINDINGS.json": json.dumps(FINDINGS),
        "TRIAGE.json": json.dumps(TRIAGE),
        "PATCHES/bug_01/meta.json": json.dumps({"finding": "f001", "title": "Identity binding"}),
        "PATCHES/bug_01/patch.diff": PATCH_1,
        "PATCHES/bug_02/meta.json": json.dumps({"finding": "f002", "title": "Traversal"}),
        "PATCHES/bug_02/patch.diff": PATCH_2,
    }


def note_archive() -> dict[str, bytes | str]:
    """The notes.md shape: the finding id lives in the note's heading."""
    return {
        "VULN-FINDINGS.json": json.dumps(FINDINGS),
        "PATCHES/bug_01/notes.md": "# bug_01 — f001: Identity binding\n\nDetail about f001.\n",
        "PATCHES/bug_01/patch.diff": PATCH_1,
        "PATCHES/bug_02/notes.md": "# bug_02 — f002: Traversal\n\nSee also f001 for context.\n",
        "PATCHES/bug_02/patch.diff": PATCH_2,
    }


class TestExpansion:
    def test_one_record_per_finding_not_per_file(self) -> None:
        result = expand(sidecar_archive())

        assert [f.finding_id for f in result.findings] == ["f001", "f002"]

    def test_the_patch_is_attached_to_its_finding(self) -> None:
        result = expand(sidecar_archive())

        by_id = {f.finding_id: f for f in result.findings}
        assert by_id["f001"].patch == PATCH_1
        assert by_id["f001"].patch_path == "PATCHES/bug_01/patch.diff"
        assert by_id["f002"].patch == PATCH_2

    def test_a_prose_note_links_by_its_heading(self) -> None:
        """Two of three real archives ship notes.md instead of a JSON sidecar."""
        result = expand(note_archive())

        by_id = {f.finding_id: f for f in result.findings}
        assert by_id["f001"].patch == PATCH_1
        assert by_id["f002"].patch == PATCH_2

    def test_a_cross_reference_does_not_steal_the_link(self) -> None:
        """bug_02's note mentions f001 in its body; the heading has to win.

        Regression: candidate ids were tried longest-first, and every id in a real
        archive is the same length, so that reduced to set iteration order and 16
        of 97 bundles latched onto a cross-referenced id.
        """
        result = expand(note_archive())

        by_id = {f.finding_id: f for f in result.findings}
        assert by_id["f002"].patch_path == "PATCHES/bug_02/patch.diff"
        assert by_id["f001"].patch_path == "PATCHES/bug_01/patch.diff"

    def test_manifests_are_merged_by_id_not_duplicated(self) -> None:
        """TRIAGE.json describes the same findings, so it must not add reports."""
        result = expand(sidecar_archive())

        assert len(result.findings) == 2
        body = next(f.body for f in result.findings if f.finding_id == "f001")
        assert "HIGH" in body, "the panel's severity should be merged in"
        assert "Client identity" in body, "and the original description kept"

    def test_the_note_is_folded_into_the_finding(self) -> None:
        result = expand(note_archive())

        f001 = next(f for f in result.findings if f.finding_id == "f001")
        assert "Detail about f001." in f001.body
        assert "PATCHES/bug_01/notes.md" in result.consumed

    def test_scan_context_rides_along(self) -> None:
        """A finding saying "line 412" needs to say which repo and commit."""
        result = expand(sidecar_archive())

        body = result.findings[0].body
        assert "apache/spark" in body
        assert "abc123" in body

    def test_consumed_covers_the_manifests_and_bundles(self) -> None:
        result = expand(sidecar_archive())

        assert "VULN-FINDINGS.json" in result.consumed
        assert "TRIAGE.json" in result.consumed
        assert "PATCHES/bug_01/patch.diff" in result.consumed
        assert "PATCHES/bug_01/meta.json" in result.consumed

    def test_patch_revisions_are_consumed_too(self) -> None:
        """patch.diff.r1 / .pre-stack are real entries; suffix tests called them
        unknown extensions and imported them as reports."""
        entries = note_archive()
        entries["PATCHES/bug_01/patch.diff.r1"] = PATCH_1 + "# revised\n"
        entries["PATCHES/bug_01/patch.diff.pre-stack"] = PATCH_1

        result = expand(entries)

        assert "PATCHES/bug_01/patch.diff.r1" in result.consumed
        assert "PATCHES/bug_01/patch.diff.pre-stack" in result.consumed

    def test_an_ordinary_archive_is_not_recognised(self) -> None:
        """No manifest, so zip_import must fall back to file-per-report."""
        result = expand({"report.txt": "A vulnerability was found.", "notes.md": "hello"})

        assert result.recognised is False
        assert result.consumed == set()

    def test_a_findings_count_is_not_a_findings_list(self) -> None:
        """ "findings": 129 would otherwise expand into nothing useful."""
        result = expand({"summary.json": json.dumps({"findings": 129, "target": "x"})})

        assert result.recognised is False

    def test_malformed_json_falls_back_rather_than_raising(self) -> None:
        result = expand({"VULN-FINDINGS.json": "{not json at all"})

        assert result.recognised is False

    def test_a_finding_without_an_id_is_skipped(self) -> None:
        """The id is the join key; without one there's nothing to attach a patch to."""
        result = expand({"m.json": json.dumps({"findings": [{"category": "auth"}, {"id": "f9"}]})})

        assert [f.finding_id for f in result.findings] == ["f9"]

    def test_a_bare_list_is_a_manifest(self) -> None:
        result = expand({"findings.json": json.dumps([{"id": "a1", "description": "boom"}])})

        assert [f.finding_id for f in result.findings] == ["a1"]

    def test_unknown_fields_are_surfaced_not_dropped(self) -> None:
        result = expand(
            {"m.json": json.dumps({"findings": [{"id": "x", "cvss_vector": "AV:N/AC:L"}]})}
        )

        assert "AV:N/AC:L" in result.findings[0].body


@pytest.mark.django_db
class TestImportIntegration:
    """The importer has to prefer findings over files, without double-storing."""

    def test_findings_become_reports_with_their_patches(self) -> None:
        result = import_reports_from_zip(
            make_zip(sidecar_archive()), require_security_content=False
        )

        assert result.imported == 2
        reports = {r.finding_id: r for r in SecurityReport.objects.all()}
        assert set(reports) == {"f001", "f002"}
        assert reports["f001"].proposed_patch == PATCH_1
        assert reports["f001"].proposed_patch_path == "PATCHES/bug_01/patch.diff"

    def test_the_manifest_is_not_also_stored_as_a_blob(self) -> None:
        import_reports_from_zip(make_zip(sidecar_archive()), require_security_content=False)

        titles = list(SecurityReport.objects.values_list("title", flat=True))
        assert not any("VULN-FINDINGS.json" in t for t in titles)

    def test_reimport_is_a_no_op(self) -> None:
        """The rendering is deterministic, so text dedup still holds."""
        import_reports_from_zip(make_zip(sidecar_archive()), require_security_content=False)
        again = import_reports_from_zip(make_zip(sidecar_archive()), require_security_content=False)

        assert again.imported == 0
        assert again.duplicates == 2
        assert SecurityReport.objects.count() == 2

    def test_titles_name_the_finding(self) -> None:
        import_reports_from_zip(make_zip(sidecar_archive()), require_security_content=False)

        titles = sorted(SecurityReport.objects.values_list("title", flat=True))
        assert titles[0].startswith("f001: ")

    def test_an_ordinary_archive_still_imports_per_file(self) -> None:
        result = import_reports_from_zip(
            make_zip({"a.txt": "A path traversal vulnerability allows arbitrary file write."})
        )

        assert result.imported == 1
        assert SecurityReport.objects.get().finding_id == ""


class TestBundleCollision:
    def test_the_losing_bundle_is_still_consumed(self) -> None:
        """Otherwise its note flows into the generic walk as a near-duplicate report.

        "keeping the first" was only half true: the second directory's prose was
        kept too, as its own row.
        """
        entries = note_archive()
        # A third directory whose note also resolves to f001.
        entries["PATCHES/bug_09/notes.md"] = "# bug_09 — f001: same finding, second dir\n"
        entries["PATCHES/bug_09/patch.diff"] = PATCH_1

        result = expand(entries)

        assert "PATCHES/bug_09/notes.md" in result.consumed
        assert "PATCHES/bug_09/patch.diff" in result.consumed


class TestRendering:
    def test_the_scan_context_gets_its_own_break(self) -> None:
        """The deliberate blank line was being filtered out with the empty fields,
        so "-- scan context --" read as another field value."""
        from franktheunicorn.security.scan_archive import _render

        body = _render("f001", {"title": "T", "description": "d"}, {"target": "repo"})

        assert "\n\n-- scan context --\n" in body


#: PATCHES.json's shape: rows about findings, under a key that isn't "findings",
#: carrying their own bug_NN id and a link to the finding they fix.
PATCH_MANIFEST = {
    "target": "apache/spark",
    "summary": {"tp": 2, "patched": 2},
    "patches": [
        {
            "id": "bug_01",
            "finding": "f001",
            "title": "Identity binding",
            "verdict": "ACCEPT",
            "approach": "Hold the claimed appId in a local field.",
        },
        {
            "id": "bug_02",
            "finding": "f002",
            "verdict": "ACCEPT",
            "approach": "Validate mergeDir at the trust boundary.",
        },
    ],
}

#: The per-finding-sections shape: a rollup with a preamble and one heading each.
#: Three findings, because a file has to anchor ``_MIN_ROLLUP_ANCHORS`` of them
#: before it counts as a rollup rather than as prose that mentions a couple.
TRIAGE_MD = """# TRIAGE — apache/spark

## Summary

| Metric | Count |
|---|---|
| Total | 3 |

## True positives

### f001 — Identity binding

- **Verdict:** true_positive (5/5 TP)
- **Panel:** every voter confirmed the pre-credential setClientId call.

### f002 — Traversal

- **Verdict:** true_positive (4/5 TP)
- **Panel:** mergeDir is attacker-supplied.

### f003 — Unescaped executor id

- **Verdict:** true_positive (5/5 TP)
- **Panel:** the value reaches an inline script raw.
"""


def rollup_archive() -> dict[str, bytes | str]:
    """:func:`sidecar_archive` plus a third finding, so a rollup can clear the
    ``_MIN_ROLLUP_ANCHORS`` floor."""
    entries = sidecar_archive()
    entries["SUPPLEMENTAL.json"] = json.dumps({"findings": [{"id": "f003", "category": "xss"}]})
    return entries


class TestCrossReferenceManifests:
    """A manifest whose rows are *about* findings rather than being them."""

    def test_a_patch_row_lands_on_its_finding(self) -> None:
        entries = sidecar_archive()
        entries["PATCHES.json"] = json.dumps(PATCH_MANIFEST)

        result = expand(entries)

        by_id = {f.finding_id: f for f in result.findings}
        assert "Hold the claimed appId in a local field." in by_id["f001"].body
        assert "Validate mergeDir at the trust boundary." in by_id["f002"].body

    def test_the_row_is_labelled_with_its_own_id(self) -> None:
        """``bug_01`` is what the archive's other files call it, so say so."""
        entries = sidecar_archive()
        entries["PATCHES.json"] = json.dumps(PATCH_MANIFEST)

        result = expand(entries)

        f001 = next(f for f in result.findings if f.finding_id == "f001")
        assert "-- PATCHES.json (bug_01) --" in f001.body

    def test_the_rows_are_not_merged_into_the_finding_fields(self) -> None:
        """PATCHES.json's "verdict: ACCEPT" is a patch review; TRIAGE.json's
        "verdict: true_positive" is the panel. Merging collapses one of them."""
        entries = sidecar_archive()
        entries["TRIAGE.json"] = json.dumps(
            {"findings": [{"id": "f001", "verdict": "true_positive"}]}
        )
        entries["PATCHES.json"] = json.dumps(PATCH_MANIFEST)

        result = expand(entries)

        f001 = next(f for f in result.findings if f.finding_id == "f001")
        assert "Verdict: true_positive" in f001.body
        assert "Verdict: ACCEPT" in f001.body

    def test_the_run_summary_survives_as_a_residual(self) -> None:
        entries = sidecar_archive()
        entries["PATCHES.json"] = json.dumps(PATCH_MANIFEST)

        result = expand(entries)

        residual = result.residuals["PATCHES.json"]
        assert '{"patched": 2, "tp": 2}' in residual
        assert "Hold the claimed appId" not in residual, "the rows moved out"

    def test_a_manifest_with_nothing_left_over_is_consumed(self) -> None:
        entries = sidecar_archive()
        entries["PATCHES.json"] = json.dumps({"patches": PATCH_MANIFEST["patches"]})

        result = expand(entries)

        assert "PATCHES.json" in result.consumed
        assert "PATCHES.json" not in result.residuals

    def test_a_row_pointing_at_an_unknown_finding_is_dropped(self) -> None:
        """Not filed under a made-up finding id."""
        entries = sidecar_archive()
        entries["PATCHES.json"] = json.dumps(
            {"patches": [{"id": "bug_99", "finding": "f999", "approach": "nowhere"}]}
        )

        result = expand(entries)

        assert all("nowhere" not in f.body for f in result.findings)


class TestRollupSplitting:
    def test_a_per_finding_section_moves_to_its_finding(self) -> None:
        entries = rollup_archive()
        entries["TRIAGE.md"] = TRIAGE_MD

        result = expand(entries)

        by_id = {f.finding_id: f for f in result.findings}
        assert "every voter confirmed" in by_id["f001"].body
        assert "mergeDir is attacker-supplied" in by_id["f002"].body
        assert "mergeDir is attacker-supplied" not in by_id["f001"].body

    def test_the_remainder_becomes_a_residual_not_a_blob(self) -> None:
        entries = rollup_archive()
        entries["TRIAGE.md"] = TRIAGE_MD

        result = expand(entries)

        residual = result.residuals["TRIAGE.md"]
        assert "| Total | 3 |" in residual, "the summary table is about the run"
        assert "every voter confirmed" not in residual, "the finding detail moved out"
        assert "attached to the matching findings" in residual, "and says where"

    def test_a_group_heading_ends_the_section_above_it(self) -> None:
        """``### f003`` must not swallow the ``## Appendix`` that follows."""
        entries = rollup_archive()
        entries["TRIAGE.md"] = TRIAGE_MD + "\n## Appendix\n\nUnrelated prose.\n"

        result = expand(entries)

        f003 = next(f for f in result.findings if f.finding_id == "f003")
        assert "Unrelated prose." not in f003.body
        assert "Unrelated prose." in result.residuals["TRIAGE.md"]

    def test_a_bullet_and_its_indented_continuation(self) -> None:
        """MAINTAINER-REPORT.md writes a finding as a bullet, not a heading."""
        entries = rollup_archive()
        entries["MAINTAINER-REPORT.md"] = (
            "# Report\n\n"
            "## Cluster 1\n\n"
            "**Root cause:** the auth wrapper gates only receive().\n\n"
            "- **f001 — MEDIUM** — identity binding.\n"
            "  Panel: 5/5 TP.\n"
            "- **f002 — MEDIUM** — traversal.\n"
            "  Panel: 4/5 TP.\n"
            "- **f003 — LOW** — unescaped executor id.\n"
            "  Panel: 3/5 TP.\n\n"
            "Closing paragraph about the cluster.\n"
        )

        result = expand(entries)

        by_id = {f.finding_id: f for f in result.findings}
        assert "Panel: 5/5 TP." in by_id["f001"].body
        assert "Panel: 4/5 TP." in by_id["f002"].body
        assert "Panel: 4/5 TP." not in by_id["f001"].body
        residual = result.residuals["MAINTAINER-REPORT.md"]
        assert "Root cause:" in residual
        assert "Closing paragraph about the cluster." in residual

    def test_a_bug_id_line_resolves_through_the_alias(self) -> None:
        """review_verdicts.txt is keyed on bug_NN, which only the bundle knows."""
        entries = rollup_archive()
        entries["PATCHES/bug_03/meta.json"] = json.dumps({"finding": "f003"})
        entries["PATCHES/bug_03/patch.diff"] = PATCH_1
        entries["review_verdicts.txt"] = (
            "# flattened blind-review verdicts\n"
            "bug_01: ACCEPT | style=5\n"
            "bug_02: ACCEPT | style=4\n"
            "bug_03: ACCEPT | style=3\n"
        )

        result = expand(entries)

        by_id = {f.finding_id: f for f in result.findings}
        assert "bug_01: ACCEPT | style=5" in by_id["f001"].body
        assert "bug_03: ACCEPT | style=3" in by_id["f003"].body
        assert "bug_01" not in by_id["f002"].body

    def test_a_table_row_is_not_an_anchor(self) -> None:
        """Cutting ``| f001 | … |`` out leaves the header rows over nothing."""
        entries = rollup_archive()
        entries["INDEX.md"] = (
            "# Index\n\n"
            "| ID | Sev |\n|---|---|\n| f001 | HIGH |\n| f002 | LOW |\n| f003 | LOW |\n\n"
            "## f001 — one\n\nDetail one.\n\n"
            "## f002 — two\n\nDetail two.\n\n"
            "## f003 — three\n\nDetail three.\n"
        )

        result = expand(entries)

        assert "| f001 | HIGH |" in result.residuals["INDEX.md"]

    def test_a_fenced_block_is_not_split(self) -> None:
        entries = rollup_archive()
        entries["HOWTO.md"] = (
            "# How to apply\n\n"
            "```\nbug_01: run this\nbug_02: then this\nbug_03: and this\n```\n\n"
            "## f001 — one\n\nDetail one.\n\n"
            "## f002 — two\n\nDetail two.\n\n"
            "## f003 — three\n\nDetail three.\n"
        )

        result = expand(entries)

        residual = result.residuals["HOWTO.md"]
        assert "bug_01: run this" in residual
        assert all("run this" not in f.body for f in result.findings)

    def test_a_file_that_merely_mentions_findings_is_left_whole(self) -> None:
        """Two cross-references is prose, not a rollup."""
        entries = rollup_archive()
        entries["notes-to-self.md"] = "See f001 and f002.\n\nf001 first.\n"

        result = expand(entries)

        assert "notes-to-self.md" not in result.residuals
        assert "notes-to-self.md" not in result.consumed

    def test_a_bundle_note_is_not_also_split(self) -> None:
        entries = note_archive()
        entries["m3.json"] = json.dumps({"findings": [{"id": "f003"}]})

        result = expand(entries)

        assert "PATCHES/bug_01/notes.md" not in result.residuals

    def test_no_manifest_means_no_text_entry_is_even_read(self) -> None:
        """The rollup pass reads every .md and .txt, which is a second read of the
        whole archive charged against the same budget. An ordinary folder of
        reports must not pay it — the pass only runs once a manifest is found."""
        entries: dict[str, bytes | str] = {
            "one.md": "# f001\n\nA vulnerability.\n",
            "two.md": "# f002\n\nAnother.\n",
        }
        archive = zipfile.ZipFile(make_zip(entries))
        read: list[str] = []

        def spy(info: zipfile.ZipInfo) -> bytes | None:
            read.append(info.filename)
            return _read_entry(archive, info)[0]

        expand_scan_archive(archive, spy)

        assert read == []

    def test_a_finding_past_the_cap_keeps_its_section_in_the_residual(self) -> None:
        """Otherwise it is cut out of the rollup and then dropped with the finding,
        and the archive's only copy of it goes missing."""
        from franktheunicorn.security import scan_archive

        ids = [f"f{n:03d}" for n in range(1, 5)]
        entries = {
            "m.json": json.dumps({"findings": [{"id": i} for i in ids]}),
            "ROLLUP.md": "".join(f"## {i} — one\n\nDetail for {i}.\n\n" for i in ids),
        }

        with patch.object(scan_archive, "MAX_FINDINGS", 3):
            result = expand(entries)

        assert [f.finding_id for f in result.findings] == ids[:3]
        assert "Detail for f004." in result.residuals["ROLLUP.md"]
        assert "Detail for f003." not in result.residuals["ROLLUP.md"]

    def test_a_runaway_section_is_capped(self) -> None:
        from franktheunicorn.security.scan_archive import MAX_ATTACHMENT_CHARS

        entries = rollup_archive()
        entries["BIG.md"] = (
            f"## f001 — one\n\n{'x' * (MAX_ATTACHMENT_CHARS * 2)}\n\n"
            "## f002 — two\n\nshort\n\n## f003 — three\n\nshort\n"
        )

        result = expand(entries)

        f001 = next(f for f in result.findings if f.finding_id == "f001")
        assert "(section truncated)" in f001.body
        assert len(f001.body) < MAX_ATTACHMENT_CHARS * 2


class TestPriority:
    def test_severity_is_the_coarse_sort(self) -> None:
        result = expand(
            {
                "m.json": json.dumps(
                    {
                        "findings": [
                            {"id": "low", "severity": "LOW"},
                            {"id": "high", "severity": "HIGH"},
                            {"id": "crit", "severity": "CRITICAL"},
                            {"id": "med", "severity": "MEDIUM"},
                        ]
                    }
                )
            }
        )

        ranked = sorted(result.findings, key=lambda f: -f.priority)
        assert [f.finding_id for f in ranked] == ["crit", "high", "med", "low"]

    def test_a_refuted_finding_sinks_below_an_unranked_one(self) -> None:
        result = expand(
            {
                "m.json": json.dumps(
                    {
                        "findings": [
                            {"id": "fp", "severity": "HIGH", "verdict": "false_positive"},
                            {"id": "plain", "severity": "LOW"},
                        ]
                    }
                )
            }
        )

        by_id = {f.finding_id: f for f in result.findings}
        assert by_id["fp"].priority < by_id["plain"].priority

    def test_confidence_cannot_lift_a_tier(self) -> None:
        """Otherwise a confidently-reported LOW outranks a hedged MEDIUM."""
        result = expand(
            {
                "m.json": json.dumps(
                    {
                        "findings": [
                            {"id": "low", "severity": "LOW", "confidence": 1.0},
                            {"id": "med", "severity": "MEDIUM", "confidence": 0.1},
                        ]
                    }
                )
            }
        )

        by_id = {f.finding_id: f for f in result.findings}
        assert by_id["med"].priority > by_id["low"].priority

    def test_a_percentage_confidence_is_not_read_as_a_fraction(self) -> None:
        result = expand(
            {
                "m.json": json.dumps(
                    {
                        "findings": [
                            {"id": "pct", "severity": "LOW", "confidence": 80},
                            {"id": "frac", "severity": "LOW", "confidence": 0.8},
                        ]
                    }
                )
            }
        )

        by_id = {f.finding_id: f for f in result.findings}
        assert by_id["pct"].priority == by_id["frac"].priority

    def test_the_reason_accounts_for_the_number(self) -> None:
        result = expand(
            {
                "m.json": json.dumps(
                    {
                        "findings": [
                            {
                                "id": "f1",
                                "severity": "HIGH",
                                "verdict": "true_positive",
                                "mean_conf": 0.79,
                                "cvss_score": 7.5,
                            }
                        ]
                    }
                )
            }
        )

        reason = result.findings[0].priority_reason
        assert reason == "HIGH, true_positive, conf 0.79, CVSS 7.5"

    def test_an_unranked_finding_gets_a_middling_score_not_zero(self) -> None:
        """Zero would put it below every false positive in the same archive."""
        result = expand({"m.json": json.dumps({"findings": [{"id": "f1", "description": "d"}]})})

        assert result.findings[0].priority > 0
        assert result.findings[0].priority_reason == ""


@pytest.mark.django_db
class TestPriorityOrdersTheImport:
    def test_rows_are_created_highest_priority_first(self) -> None:
        """The worker claims WorkerCommands by created_at, so insertion order is
        triage order."""
        entries = {
            "VULN-FINDINGS.json": json.dumps(
                {
                    "findings": [
                        {"id": "f001", "severity": "LOW", "description": "a vulnerability"},
                        {"id": "f002", "severity": "HIGH", "description": "a vulnerability"},
                        {"id": "f003", "severity": "MEDIUM", "description": "a vulnerability"},
                    ]
                }
            )
        }

        import_reports_from_zip(make_zip(entries), require_security_content=False)

        order = list(SecurityReport.objects.order_by("pk").values_list("finding_id", flat=True))
        assert order == ["f002", "f003", "f001"]

    def test_the_priority_and_its_reason_are_stored(self) -> None:
        entries = {
            "VULN-FINDINGS.json": json.dumps(
                {"findings": [{"id": "f001", "severity": "HIGH", "verdict": "true_positive"}]}
            )
        }

        import_reports_from_zip(make_zip(entries), require_security_content=False)

        report = SecurityReport.objects.get()
        assert report.priority == 80.0
        assert report.priority_reason == "HIGH, true_positive"


@pytest.mark.django_db
class TestResidualImport:
    def test_the_rollup_imports_as_its_remainder(self) -> None:
        entries = rollup_archive()
        entries["TRIAGE.md"] = TRIAGE_MD

        import_reports_from_zip(make_zip(entries), require_security_content=False)

        rollup = SecurityReport.objects.get(finding_id="")
        assert "| Total | 3 |" in rollup.raw_text
        assert "every voter confirmed" not in rollup.raw_text
        assert len(rollup.raw_text) < len(TRIAGE_MD)

    def test_a_residual_is_exempt_from_the_keyword_gate(self) -> None:
        """Its per-finding sections carried the keywords and have moved out; the
        apply-order narrative left behind names no vulnerability."""
        entries = rollup_archive()
        entries["PATCHES/composition.md"] = (
            "# Apply order\n\nApply in ascending order against a pristine checkout.\n\n"
            "## f001 — one\n\nTouches Foo.java.\n\n"
            "## f002 — two\n\nTouches Bar.java.\n\n"
            "## f003 — three\n\nAlso Foo.java.\n"
        )

        result = import_reports_from_zip(make_zip(entries), require_security_content=True)

        assert [e.outcome for e in result.entries if e.name == "PATCHES/composition.md"] == [
            "imported"
        ]

    def test_reimport_of_a_split_archive_is_still_a_no_op(self) -> None:
        entries = rollup_archive()
        entries["TRIAGE.md"] = TRIAGE_MD
        entries["PATCHES.json"] = json.dumps(PATCH_MANIFEST)

        first = import_reports_from_zip(make_zip(entries), require_security_content=False)
        again = import_reports_from_zip(make_zip(entries), require_security_content=False)

        assert again.imported == 0
        assert again.duplicates == first.imported


@pytest.mark.django_db
class TestFindingDedupAcrossProjects:
    def test_a_second_pass_with_a_project_does_not_duplicate_findings(self) -> None:
        """Measured before the fix: 3 findings became 6 rows, while a plain text
        file in the same archive correctly deduped."""
        from tests.factories import ProjectFactory

        entries = sidecar_archive()
        import_reports_from_zip(make_zip(entries), require_security_content=False)
        result = import_reports_from_zip(
            make_zip(entries), project=ProjectFactory(), require_security_content=False
        )

        assert result.imported == 0
        assert result.duplicates == 2
        assert SecurityReport.objects.exclude(finding_id="").count() == 2


class TestProvenance:
    """PROVENANCE.md is run-level context, not a report."""

    _TEXT = (
        "# Provenance — apache/spark @ branch branch-3.5\n"
        "- True repo: https://github.com/apache/spark — pinned commit a025e49a\n"
    )

    def test_provenance_lands_on_every_finding_and_is_consumed(self) -> None:
        """It has no security keywords, so the generic walk rejected it as
        not-a-report and dropped the only record of which commit was scanned."""
        result = expand(
            {
                "VULN-FINDINGS.json": json.dumps(FINDINGS),
                "PROVENANCE.md": self._TEXT,
            }
        )
        assert len(result.findings) == 2
        for finding in result.findings:
            assert "pinned commit a025e49a" in finding.body
            assert "branch-3.5" in finding.body
        assert "PROVENANCE.md" in result.consumed

    def test_a_huge_provenance_is_truncated(self) -> None:
        """It is repeated per finding and goes into every triage prompt."""
        from franktheunicorn.security.scan_archive import MAX_PROVENANCE_CHARS

        result = expand(
            {
                "VULN-FINDINGS.json": json.dumps(FINDINGS),
                "PROVENANCE.md": "commit a025e49a\n" + "x" * (MAX_PROVENANCE_CHARS * 2),
            }
        )
        assert "(truncated" in result.findings[0].body
        assert result.findings[0].body.count("x") <= MAX_PROVENANCE_CHARS

    def test_no_provenance_entry_changes_nothing(self) -> None:
        result = expand({"VULN-FINDINGS.json": json.dumps(FINDINGS)})
        assert len(result.findings) == 2
        assert "Provenance:" not in result.findings[0].body


class TestProvenanceEdgeCases:
    """The three cases the first round of provenance tests didn't cover."""

    def test_a_provenance_that_anchors_findings_is_split_not_pasted_whole(self) -> None:
        """Taking it before the rollup split put f002's and f003's detail on f001."""
        rollup = (
            "# Provenance\n"
            "## f001 — first\nDetail for one.\n"
            "## f002 — second\nDetail for two.\n"
            "## f003 — third\nDetail for three.\n"
        )
        findings = {
            "findings": [
                {"id": "f001", "title": "one"},
                {"id": "f002", "title": "two"},
                {"id": "f003", "title": "three"},
            ]
        }
        result = expand({"VULN-FINDINGS.json": json.dumps(findings), "PROVENANCE.md": rollup})
        bodies = {f.finding_id: f.body for f in result.findings}
        assert "Detail for one" in bodies["f001"]
        assert "Detail for two" not in bodies["f001"]
        assert "Detail for three" not in bodies["f001"]

    def test_the_top_level_provenance_wins_over_a_nested_one(self) -> None:
        """A per-patch provenance.txt sorts before PROVENANCE.md by plain name-sort,
        so the document naming the branch was never attached."""
        result = expand(
            {
                "VULN-FINDINGS.json": json.dumps(FINDINGS),
                "PATCHES/bug_01/provenance.txt": "per-patch note, not the archive's",
                "PROVENANCE.md": "the archive's own: branch-3.5, commit a025e49a",
            }
        )
        body = result.findings[0].body
        assert "commit a025e49a" in body
        assert "per-patch note" not in body

    def test_an_oversized_provenance_is_still_imported_whole(self) -> None:
        """Truncating and consuming would silently lose bytes the operator had
        before, with no per-entry outcome to notice it by."""
        from franktheunicorn.security.scan_archive import MAX_PROVENANCE_CHARS

        text = "commit a025e49a\n" + "x" * (MAX_PROVENANCE_CHARS * 2)
        result = expand({"VULN-FINDINGS.json": json.dumps(FINDINGS), "PROVENANCE.md": text})

        assert "(truncated" in result.findings[0].body
        # Not consumed: the full text is left for the generic walk to import.
        assert "PROVENANCE.md" not in result.consumed
        assert result.residuals["PROVENANCE.md"] == text.strip()
