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
