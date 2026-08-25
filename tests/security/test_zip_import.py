"""Tests for bulk security-report import from a zip archive."""

from __future__ import annotations

import io
import zipfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.core.models import SecurityReport, WorkerCommand
from franktheunicorn.security.zip_import import (
    MAX_ENTRIES,
    MAX_ENTRY_BYTES,
    import_reports_from_zip,
)
from tests.factories import ProjectFactory, SecurityReportFactory

# Reads as an actual security report: the importer applies the same
# is_security_report filter the email door does, so fixture text has to clear it.
REPORT_TEXT = (
    "There is a path traversal vulnerability in the archive extractor.\n"
    "Passing ../../etc/passwd as a member name writes outside the target dir.\n"
    "The exploit allows arbitrary file write.\n"
)

FORWARDED_EML = (
    b"From: Security Team <security@apache.org>\r\n"
    b"To: maintainer@example.com\r\n"
    b"Subject: [SECURITY] Path traversal in extractor\r\n"
    b"Message-ID: <report-1@example.com>\r\n"
    b"\r\n"
    b"A vulnerability was reported in the extractor.\r\n"
)


def _stub_message(body: str) -> Any:
    """A minimal InboxMessage, for asserting on the parse-failure path."""
    from franktheunicorn.data_access.email_inbox.types import InboxMessage

    return InboxMessage(body=body, is_security_report=True)


def zip_bytes_with_forged_sizes(entries: dict[str, bytes]) -> bytes:
    """A deflate archive whose headers under-report every entry's real size.

    Forged to exactly the per-entry cap, which is the worst case an attacker can
    reach: any lower and CPython's own ``_left`` truncation bounds the work for
    us, any higher and the cheap size gate refuses it unread. At the cap the entry
    decompresses a full 4 MiB, fails CRC, and is reported as an error — so if the
    aggregate budget isn't charged for rejected entries, that 4 MiB was free and
    MAX_ENTRIES of them are free too.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    raw = buf.getvalue()
    for data in entries.values():
        raw = raw.replace(len(data).to_bytes(4, "little"), MAX_ENTRY_BYTES.to_bytes(4, "little"))
    return raw


def make_zip(entries: dict[str, bytes | str]) -> io.BytesIO:
    """An in-memory zip, so no test touches the filesystem."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            data = content.encode() if isinstance(content, str) else content
            zf.writestr(name, data)
    buf.seek(0)
    return buf


@pytest.fixture
def no_auto_triage() -> Any:
    """Default the operator config to auto-triage off.

    Keeps the import tests about importing; the queueing gate has its own tests
    below. Without this they'd depend on whatever config the test env resolves.
    """
    from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig

    with patch("franktheunicorn.config.loader.get_operator_config") as mock:
        mock.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, auto_triage=False),
        )
        yield mock


@pytest.mark.django_db
class TestImportReportsFromZip:
    def test_imports_one_report_per_text_file(self, no_auto_triage: Any) -> None:
        archive = make_zip(
            {"one.txt": REPORT_TEXT, "two.md": REPORT_TEXT + "Second distinct exploit path."}
        )

        result = import_reports_from_zip(archive)

        assert result.error == ""
        assert result.imported == 2
        assert SecurityReport.objects.count() == 2
        assert set(SecurityReport.objects.values_list("source", flat=True)) == {"zip"}

    def test_falls_back_to_the_file_name_for_a_title(self, no_auto_triage: Any) -> None:
        """Beats "Untitled Report" when the text carries no recoverable subject."""
        result = import_reports_from_zip(make_zip({"reports/2024-03-traversal.txt": REPORT_TEXT}))

        assert result.imported == 1
        report = SecurityReport.objects.get()
        assert report.title == "2024-03-traversal.txt"

    def test_eml_entry_keeps_the_message_id_and_sender(self, no_auto_triage: Any) -> None:
        result = import_reports_from_zip(make_zip({"inbox/report.eml": FORWARDED_EML}))

        assert result.imported == 1
        report = SecurityReport.objects.get()
        assert report.email_message_id == "<report-1@example.com>"
        assert report.reporter_email == "security@apache.org"
        assert "Path traversal in extractor" in report.title

    def test_attaches_the_given_project(self, no_auto_triage: Any) -> None:
        project = ProjectFactory()

        import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}), project=project)

        assert SecurityReport.objects.get().project_id == project.pk

    def test_reimporting_the_same_archive_creates_nothing_new(self, no_auto_triage: Any) -> None:
        """The recovery path after a half-finished run must be idempotent."""
        import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}))
        result = import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}))

        assert result.imported == 0
        assert result.duplicates == 1
        assert SecurityReport.objects.count() == 1

    def test_two_copies_inside_one_archive_collapse(self, no_auto_triage: Any) -> None:
        """Rows created during the walk join the index, so intra-archive dupes dedup too."""
        result = import_reports_from_zip(
            make_zip({"a.txt": REPORT_TEXT, "nested/a-copy.txt": REPORT_TEXT})
        )

        assert result.imported == 1
        assert result.duplicates == 1
        assert SecurityReport.objects.count() == 1

    def test_dedup_does_not_scale_queries_with_entry_count(self, no_auto_triage: Any) -> None:
        """The point of the index: one pass, not a full-table text scan per entry.

        A filter(raw_text=...) per entry is O(entries x rows) over whole report
        bodies, which is exactly the shape the bulk path hits.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(5):
            SecurityReportFactory(raw_text=f"pre-existing report {i}")

        small = make_zip({"a.txt": REPORT_TEXT})
        with CaptureQueriesContext(connection) as ctx:
            import_reports_from_zip(small)
        one_entry = len(ctx.captured_queries)

        large = make_zip({f"r{i}.txt": f"{REPORT_TEXT} variant {i}" for i in range(10)})
        with CaptureQueriesContext(connection) as ctx:
            import_reports_from_zip(large)
        ten_entries = len(ctx.captured_queries)

        # Ten entries cost ten inserts more, not ten inserts *and* ten lookups.
        assert ten_entries - one_entry <= 10

    def test_dedups_an_eml_on_message_id(self, no_auto_triage: Any) -> None:
        """A Message-ID beats the text comparison — different body, same message."""
        SecurityReportFactory(
            project=None, email_message_id="<report-1@example.com>", raw_text="different text"
        )

        result = import_reports_from_zip(make_zip({"r.eml": FORWARDED_EML}))

        assert result.duplicates == 1
        assert SecurityReport.objects.count() == 1

    def test_message_id_dedup_is_scoped_to_the_target_project(self, no_auto_triage: Any) -> None:
        """Both key shapes are per-project, so a re-import doesn't split an archive.

        Keying Message-IDs globally while keying text per-project meant
        re-importing with --project refused the .eml entries as "already present"
        and duplicated the .txt ones — half the archive honouring the operator's
        intent and half not.
        """
        project = ProjectFactory()
        archive_entries: dict[str, bytes | str] = {"r.eml": FORWARDED_EML, "r.txt": REPORT_TEXT}

        first = import_reports_from_zip(make_zip(dict(archive_entries)))
        second = import_reports_from_zip(make_zip(dict(archive_entries)), project=project)

        assert first.imported == 2
        # Both entries agree: neither is "already present" under the new project.
        assert second.imported == 2
        assert second.duplicates == 0
        assert SecurityReport.objects.filter(project=project).count() == 2

    def test_same_text_under_different_projects_is_not_a_duplicate(
        self, no_auto_triage: Any
    ) -> None:
        one, two = ProjectFactory(), ProjectFactory()
        import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}), project=one)

        result = import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}), project=two)

        assert result.imported == 1
        assert SecurityReport.objects.count() == 2

    def test_skips_directories_dotfiles_and_unsupported_types(self, no_auto_triage: Any) -> None:
        archive = make_zip(
            {
                "reports/": b"",
                "reports/good.txt": REPORT_TEXT,
                "reports/.DS_Store": b"\x00\x01",
                "reports/screenshot.png": b"\x89PNG\r\n",
            }
        )

        result = import_reports_from_zip(archive)

        assert result.imported == 1
        outcomes = {e.name: e.outcome for e in result.entries}
        assert "reports/" not in outcomes
        assert "reports/.DS_Store" not in outcomes
        assert outcomes["reports/screenshot.png"] == "unsupported"

    def test_a_private_key_is_not_imported_as_a_report(self, no_auto_triage: Any) -> None:
        """PEM is text, so content sniffing passes it — the keyword filter is what stops it.

        A directory-shaped handover archive is the motivating case: source files,
        a screenshot, and an id_ed25519 that would otherwise land in the reports
        table and be queued for an LLM to read.
        """
        key = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
        )
        archive = make_zip(
            {"id_ed25519": key, "Makefile": "all:\n\tpytest\n", "r.txt": REPORT_TEXT}
        )

        result = import_reports_from_zip(archive)

        outcomes = {e.name: e.outcome for e in result.entries}
        assert outcomes["id_ed25519"] == "not-a-report"
        assert outcomes["Makefile"] == "not-a-report"
        assert outcomes["r.txt"] == "imported"
        assert SecurityReport.objects.count() == 1
        assert not SecurityReport.objects.filter(raw_text__contains="PRIVATE KEY").exists()

    def test_the_filter_can_be_turned_off(self, no_auto_triage: Any) -> None:
        result = import_reports_from_zip(
            make_zip({"notes.txt": "Just some ordinary notes."}),
            require_security_content=False,
        )

        assert result.imported == 1

    def test_binary_content_is_refused_whatever_the_name_says(self, no_auto_triage: Any) -> None:
        """Content vetoes the extension: a .txt full of NULs is not a report."""
        result = import_reports_from_zip(make_zip({"report.txt": b"\x00\x01\x02binary\x00"}))

        assert [e.outcome for e in result.entries] == ["unsupported"]
        assert SecurityReport.objects.count() == 0

    def test_a_subject_shaped_name_is_not_mistaken_for_an_extension(
        self, no_auto_triage: Any
    ) -> None:
        """ "...v1.2.3" used to classify as type '.3' and get dropped as unsupported."""
        name = "Re: [SECURITY] traversal in v1.2.3"

        result = import_reports_from_zip(make_zip({name: REPORT_TEXT}))

        assert result.imported == 1
        assert [e.outcome for e in result.entries] == ["imported"]

    def test_an_mbox_is_refused_rather_than_silently_collapsed(self, no_auto_triage: Any) -> None:
        """parse_email_message reads one message, so N-1 would vanish into a body."""
        mbox = (
            b"From reporter@example.com Mon Jan  1 00:00:00 2024\r\n"
            b"Subject: [SECURITY] first vulnerability exploit\r\n\r\nBody one\r\n\r\n"
            b"From other@example.com Mon Jan  2 00:00:00 2024\r\n"
            b"Subject: [SECURITY] second vulnerability exploit\r\n\r\nBody two\r\n"
        )

        result = import_reports_from_zip(make_zip({"inbox.mbox": mbox}))

        assert [e.outcome for e in result.entries] == ["unsupported"]
        assert SecurityReport.objects.count() == 0

    def test_a_partial_import_is_not_reported_as_a_total_failure(self, no_auto_triage: Any) -> None:
        """Rows are already committed when a cap trips, so "failed" alone hides them."""
        from franktheunicorn.security import zip_import as zi

        archive = make_zip({f"r{i}.txt": f"{REPORT_TEXT} variant {i}" for i in range(4)})

        with patch.object(zi, "MAX_TOTAL_BYTES", len(REPORT_TEXT) * 2):
            result = import_reports_from_zip(archive)

        assert result.error
        assert result.imported >= 1
        assert "imported" in result.summary()
        assert "then stopped" in result.summary()

    def test_blank_entry_is_recorded_not_imported(self, no_auto_triage: Any) -> None:
        result = import_reports_from_zip(make_zip({"empty.txt": "   \n\n  "}))

        assert result.imported == 0
        assert result.skipped == 1
        assert [e.outcome for e in result.entries] == ["empty"]
        assert SecurityReport.objects.count() == 0

    def test_a_non_zip_file_is_an_error_not_an_exception(self, no_auto_triage: Any) -> None:
        result = import_reports_from_zip(io.BytesIO(b"this is not a zip"))

        assert result.error == "not a valid zip archive"
        assert result.imported == 0
        assert "Import failed" in result.summary()

    def test_honestly_oversized_entry_is_refused_from_the_header(self, no_auto_triage: Any) -> None:
        archive = make_zip({"huge.txt": "A" * (MAX_ENTRY_BYTES + 1), "ok.txt": REPORT_TEXT})

        result = import_reports_from_zip(archive)

        outcomes = {e.name: e.outcome for e in result.entries}
        assert outcomes["huge.txt"] == "too-large"
        assert outcomes["ok.txt"] == "imported"

    def test_a_lying_header_does_not_get_to_decompress_without_limit(
        self, no_auto_triage: Any
    ) -> None:
        """The header is the attacker's to write, so it cannot be the limit.

        CPython truncates to the declared file_size only *after* decompressing
        (zipfile.py: `data = data[:self._left]`), so `ZipFile.read` on an entry
        claiming a few bytes over a huge payload still expands the whole thing.
        The bound has to be applied to the read itself.
        """
        payload = b"A" * (MAX_ENTRY_BYTES * 2)
        archive = make_zip({"bomb.txt": payload, "ok.txt": REPORT_TEXT})

        # Forge the central directory + local header to under-report the size.
        raw = bytearray(archive.getvalue())
        true_size = len(payload).to_bytes(4, "little")
        lie = (64).to_bytes(4, "little")
        assert raw.count(true_size) >= 2, "expected size in both local and central headers"
        raw = bytearray(bytes(raw).replace(true_size, lie))

        reads: list[int] = []
        real_read = zipfile.ZipExtFile.read

        def watched(self: Any, n: int = -1) -> bytes:
            reads.append(n)
            return real_read(self, n)

        with patch.object(zipfile.ZipExtFile, "read", watched):
            result = import_reports_from_zip(io.BytesIO(bytes(raw)))

        # Never an unbounded read: every call carries an explicit cap.
        assert reads and all(n is not None and n > 0 for n in reads)
        # The bomb is refused or errors out; it is never imported as a report.
        outcomes = {e.name: e.outcome for e in result.entries}
        assert outcomes.get("bomb.txt") in ("too-large", "error")
        assert not SecurityReport.objects.filter(raw_text__startswith="AAAA").exists()

    def test_total_expansion_cap_stops_the_import(self, no_auto_triage: Any) -> None:
        """Many individually-legal entries must not add up to an unbounded read."""
        from franktheunicorn.security import zip_import

        archive = make_zip({f"r{i}.txt": REPORT_TEXT + str(i) for i in range(4)})

        with patch.object(zip_import, "MAX_TOTAL_BYTES", len(REPORT_TEXT) * 2):
            result = import_reports_from_zip(archive)

        assert "total limit" in result.error
        # Stops at the cap rather than throwing away what it already imported.
        assert SecurityReport.objects.count() == result.imported < 4

    def test_an_unparseable_entry_is_recorded_and_the_rest_import(
        self, no_auto_triage: Any
    ) -> None:
        archive = make_zip(
            {"a.txt": REPORT_TEXT, "b.txt": REPORT_TEXT + "Another distinct exploit."}
        )

        with patch(
            "franktheunicorn.data_access.email_inbox.parser.parse_pasted_report",
            side_effect=[ValueError("parser blew up"), _stub_message(REPORT_TEXT)],
        ):
            result = import_reports_from_zip(archive)

        outcomes = {e.name: e.outcome for e in result.entries}
        assert outcomes["a.txt"] == "error"
        assert outcomes["b.txt"] == "imported"
        assert SecurityReport.objects.count() == 1

    def test_bzip2_entries_are_refused_outright(self, no_auto_triage: Any) -> None:
        """CPython does not bound bzip2/lzma decompression, so the cap can't apply.

        zipfile._read1 calls decompress(data, n) for deflate but bare
        decompress(data) for bzip2/lzma, expanding everything before the declared
        size is applied — so a tiny archive could drive gigabytes. Every other
        test here uses ZIP_DEFLATED, which is why nothing caught it.
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_BZIP2) as zf:
            zf.writestr("r.txt", REPORT_TEXT)
        buf.seek(0)

        result = import_reports_from_zip(buf)

        # Refused on the codec, before any read — even though it is a perfectly
        # well-formed, small, genuine report.
        assert [(e.outcome, e.detail) for e in result.entries] == [
            ("unsupported", "unsupported compression (type 12)")
        ]
        assert SecurityReport.objects.count() == 0

    def test_rejected_entries_are_charged_against_the_total_budget(
        self, no_auto_triage: Any
    ) -> None:
        """Otherwise refusing an entry is free and the aggregate cap bounds nothing."""
        from franktheunicorn.security import zip_import as zi

        # Headers that under-report: each passes the cheap size gate, then really
        # decompresses up to the cap before being refused. An honest oversized
        # header costs nothing and is correctly charged nothing, so the attack
        # this guards against is specifically the lying one.
        body = b"A" * (MAX_ENTRY_BYTES * 2)
        raw = bytearray(zip_bytes_with_forged_sizes({f"r{i}.txt": body for i in range(4)}))

        with patch.object(zi, "MAX_TOTAL_BYTES", MAX_ENTRY_BYTES * 2):
            result = import_reports_from_zip(io.BytesIO(bytes(raw)))

        assert result.error, "aggregate budget must trip on rejected entries too"
        # And it stops rather than grinding through all four.
        assert len(result.entries) < 4

    def test_binary_extensions_are_rejected_before_any_decompression(
        self, no_auto_triage: Any
    ) -> None:
        """A screenshot must not spend the aggregate budget, or order decides the import."""
        from franktheunicorn.security import zip_import as zi

        png = b"\x89PNG\r\n" + b"\x00" * (512 * 1024)
        archive = make_zip(
            {"aaa1.png": png, "aaa2.png": png, "aaa3.png": png, "zz-report.txt": REPORT_TEXT}
        )

        with patch.object(zi, "MAX_TOTAL_BYTES", 256 * 1024):
            result = import_reports_from_zip(archive)

        # The report sorts last, so if PNGs were charged it would never be reached.
        assert result.imported == 1
        assert result.error == ""
        # Every entry is accounted for, none silently dropped.
        assert len(result.entries) == 4

    def test_a_non_ascii_message_id_does_not_drop_the_report(self, no_auto_triage: Any) -> None:
        """compat32 wraps an 8-bit header in email.header.Header, which can't be sliced."""
        eml = (
            b"From: Reporter <reporter@example.com>\r\n"
            b"Subject: [SECURITY] traversal exploit\r\n"
            b"Message-ID: <\xc3\xbcid@example.com>\r\n"
            b"\r\nA path traversal vulnerability with a working exploit.\r\n"
        )

        result = import_reports_from_zip(make_zip({"r.eml": eml}))

        assert result.imported == 1, [e.detail for e in result.entries]
        assert SecurityReport.objects.count() == 1

    def test_utf16_reports_are_not_mistaken_for_binaries(self, no_auto_triage: Any) -> None:
        """UTF-16 has a NUL per ASCII char, so the binary heuristic needs the BOM."""
        archive = make_zip(
            {
                "bom.txt": REPORT_TEXT.encode("utf-16"),
                "le.txt": (REPORT_TEXT + "variant").encode("utf-16-le"),
            }
        )

        result = import_reports_from_zip(archive)

        # The BOM'd one must import and read as text, not replacement characters.
        outcomes = {e.name: e.outcome for e in result.entries}
        assert outcomes["bom.txt"] == "imported", [e.detail for e in result.entries]
        assert "path traversal vulnerability" in SecurityReport.objects.get().raw_text

    def test_a_long_message_id_still_dedups(self, no_auto_triage: Any) -> None:
        """The key was built from the full id, the column stores 500 chars."""
        long_id = "<" + "x" * 620 + "@example.com>"
        eml = (
            b"From: Sec <security@apache.org>\r\n"
            b"Subject: [SECURITY] traversal\r\n"
            b"Message-ID: " + long_id.encode() + b"\r\n\r\n"
            b"A vulnerability: path traversal allows arbitrary file write.\r\n"
        )
        # Bodies differ, so only the message-id door can catch this.
        other = eml.replace(b"arbitrary file write", b"arbitrary file read")

        import_reports_from_zip(make_zip({"a.eml": eml}))
        result = import_reports_from_zip(make_zip({"b.eml": other}))

        assert result.imported == 0
        assert result.duplicates == 1
        assert SecurityReport.objects.count() == 1

    def test_a_nul_below_the_sniff_window_does_not_reach_the_database(
        self, no_auto_triage: Any
    ) -> None:
        """_looks_binary only scans 8 KiB, and a NUL is valid UTF-8.

        A report with a core-dump or log paste past that mark kept them. SQLite
        stores a NUL, psycopg refuses one, so the two backends disagreed about
        whether the entry imports — and it rode into the triage prompt either way.
        """
        raw = REPORT_TEXT.encode() + b"log line padding\n" * 700 + b"\x00tail of a core dump"

        result = import_reports_from_zip(make_zip({"crash.txt": raw}))

        assert result.imported == 1
        assert "\x00" not in SecurityReport.objects.get().raw_text

    def test_an_encrypted_archive_says_it_is_encrypted(self, no_auto_triage: Any) -> None:
        """Every entry failed CRC, so this read as N copies of "corrupt"."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("a.txt", REPORT_TEXT)
        raw = bytearray(buf.getvalue())
        # Set bit 0 of the general-purpose flags in both the local header and the
        # central directory, which is what a password-protected entry carries.
        for sig in (b"PK\x03\x04", b"PK\x01\x02"):
            at = raw.find(sig)
            offset = 6 if sig == b"PK\x03\x04" else 8
            raw[at + offset] |= 0x1

        result = import_reports_from_zip(io.BytesIO(bytes(raw)))

        assert result.imported == 0
        assert [e.outcome for e in result.entries] == ["unsupported"]
        assert "encrypted" in result.entries[0].detail

    def test_backslash_paths_do_not_smuggle_junk_past_the_filters(
        self, no_auto_triage: Any
    ) -> None:
        """Both checks in _is_ignorable are per-segment, and it split on "/" only."""
        archive = make_zip(
            {
                "__MACOSX\\._report.txt": REPORT_TEXT,
                "notes\\.DS_Store": REPORT_TEXT,
                "real.txt": REPORT_TEXT,
            }
        )

        result = import_reports_from_zip(archive)

        assert [e.name for e in result.entries] == ["real.txt"]
        assert result.imported == 1

    def test_over_the_entry_cap_is_flagged_not_just_worded(self, no_auto_triage: Any) -> None:
        """The dashboard used to sniff a substring of result.error for this."""
        archive = make_zip({f"r{i}.txt": f"{REPORT_TEXT} variant {i}" for i in range(5)})

        result = import_reports_from_zip(archive, max_entries=2)

        assert result.over_entry_cap is True
        assert result.imported == 0

    def test_a_binary_starting_with_a_bom_is_still_refused(self, no_auto_triage: Any) -> None:
        """Accepting on the BOM alone was two bytes and no scan."""
        archive = make_zip({"trap": b"\xff\xfe" + bytes(range(256)) * 40})

        result = import_reports_from_zip(archive)

        assert result.imported == 0
        assert [e.outcome for e in result.entries] == ["unsupported"]

    def test_a_utf16_eml_keeps_its_headers(self, no_auto_triage: Any) -> None:
        """Raw UTF-16 is unreadable to the MIME parser, so this used to vanish."""
        raw = b"\xff\xfe" + FORWARDED_EML.decode().encode("utf-16-le")

        result = import_reports_from_zip(make_zip({"export.eml": raw}))

        assert result.imported == 1, [e.outcome for e in result.entries]
        report = SecurityReport.objects.get()
        assert report.email_message_id == "<report-1@example.com>"
        assert "Path traversal in extractor" in report.title

    def test_a_utf8_bom_eml_keeps_its_headers(self, no_auto_triage: Any) -> None:
        """The BOM sat where the first header name goes, so the parser saw only a body.

        It still imported, which is why nothing flagged it: no reporter, no date,
        no message-id to dedup on. Notepad writes this by default.
        """
        result = import_reports_from_zip(make_zip({"export.eml": b"\xef\xbb\xbf" + FORWARDED_EML}))

        assert result.imported == 1
        report = SecurityReport.objects.get()
        assert report.email_message_id == "<report-1@example.com>"
        assert report.reporter_email == "security@apache.org"

    def test_a_bom_does_not_defeat_text_dedup(self, no_auto_triage: Any) -> None:
        """U+FEFF is category Cf, not whitespace, so strip() left it in the sha256."""
        archive = make_zip(
            {"plain.txt": REPORT_TEXT.encode(), "bom.txt": b"\xef\xbb\xbf" + REPORT_TEXT.encode()}
        )

        result = import_reports_from_zip(archive)

        assert result.imported == 1
        assert result.duplicates == 1
        assert not SecurityReport.objects.get().raw_text.startswith("﻿")

    def test_too_many_entries_is_refused_up_front(self, no_auto_triage: Any) -> None:
        archive = make_zip({f"r{i}.txt": "x" for i in range(MAX_ENTRIES + 1)})

        result = import_reports_from_zip(archive)

        assert "over the" in result.error
        assert SecurityReport.objects.count() == 0

    def test_an_unreadable_entry_does_not_stop_the_rest(self, no_auto_triage: Any) -> None:
        archive = make_zip(
            {"a.txt": REPORT_TEXT, "b.txt": REPORT_TEXT + "Another distinct exploit."}
        )
        real_open = zipfile.ZipFile.open

        def flaky(self: zipfile.ZipFile, member: Any, *a: Any, **k: Any) -> Any:
            name = member.filename if hasattr(member, "filename") else member
            if name == "a.txt":
                raise OSError("disk gremlins")
            return real_open(self, member, *a, **k)

        with patch.object(zipfile.ZipFile, "open", flaky):
            result = import_reports_from_zip(archive)

        assert result.imported == 1
        assert result.failed == 1
        assert SecurityReport.objects.count() == 1

    def test_a_failed_insert_does_not_abandon_the_remaining_entries(
        self, no_auto_triage: Any
    ) -> None:
        """create() was the one unguarded call, so a DataError killed the walk."""
        from django.db import DatabaseError

        archive = make_zip({f"r{i}.txt": f"{REPORT_TEXT} variant {i}" for i in range(3)})
        real_create = SecurityReport.objects.create
        calls = {"n": 0}

        def flaky_create(**kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                raise DatabaseError("database is locked")
            return real_create(**kwargs)

        with patch.object(SecurityReport.objects, "create", flaky_create):
            result = import_reports_from_zip(archive)

        assert result.error == ""  # not reported as a whole-archive failure
        assert result.imported == 2
        assert result.failed == 1
        assert len(result.entries) == 3  # every entry still accounted for

    def test_overlong_eml_headers_are_truncated_to_the_column_widths(
        self, no_auto_triage: Any
    ) -> None:
        """Untruncated these are silently over-long on SQLite and a DataError on Postgres."""
        long_name = "N" * 600
        eml = (
            f"From: {long_name} <reporter@example.com>\r\n"
            f"Subject: [SECURITY] overflow\r\n"
            f"Message-ID: <{'x' * 600}@example.com>\r\n"
            f"\r\nA vulnerability in the parser.\r\n"
        ).encode()

        result = import_reports_from_zip(make_zip({"r.eml": eml}))

        assert result.imported == 1
        report = SecurityReport.objects.get()
        assert len(report.reporter_name) <= 255
        assert len(report.reporter_email) <= 255
        assert len(report.email_message_id) <= 500
        assert len(report.title) <= 500

    def test_undecodable_bytes_still_import(self, no_auto_triage: Any) -> None:
        """A mangled byte is no reason to drop a report on the floor."""
        result = import_reports_from_zip(
            make_zip({"r.txt": REPORT_TEXT.encode() + b" \xff\xfe trailing"})
        )

        assert result.imported == 1
        assert "path traversal vulnerability" in SecurityReport.objects.get().raw_text


@pytest.mark.django_db
class TestImportAutoTriage:
    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_bulk_import_does_not_triage_just_because_the_config_says_so(
        self, mock_config: MagicMock
    ) -> None:
        """auto_triage is for a trickle of email; a backlog has to be asked for.

        Every imported report costs an NVD lookup and two LLM calls, so the
        operator's per-report setting must not silently authorise a thousand of
        them at once.
        """
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, auto_triage=True),
        )

        result = import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}))

        assert result.imported == 1
        assert result.queued_triage == 0
        assert WorkerCommand.objects.count() == 0

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_queues_triage_when_the_caller_opts_in(self, mock_config: MagicMock) -> None:
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, auto_triage=True),
        )

        result = import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}), auto_triage=True)

        assert result.queued_triage == 1
        assert WorkerCommand.objects.filter(command="run_security_triage").count() == 1
        assert "queued for triage" in result.summary()

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_opting_in_is_not_vetoed_by_auto_triage_being_off(self, mock_config: MagicMock) -> None:
        """auto_triage means "triage on arrival", not "may triage at all".

        Applying it to an explicit --triage / ticked box made both a silent
        no-op on exactly the installs most likely to be asking by hand.
        """
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, auto_triage=False),
        )

        result = import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}), auto_triage=True)

        assert result.imported == 1
        assert result.queued_triage == 1
        assert WorkerCommand.objects.filter(command="run_security_triage").count() == 1

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_the_feature_flag_stops_bulk_triage_but_says_so(self, mock_config: MagicMock) -> None:
        """enabled:false has to mean something for a 2000x fan-out.

        Unlike the single-report button, where the click is the consent. Nothing
        downstream re-checks — neither the worker dispatcher nor triage_report —
        so if this doesn't hold the line, nothing does. Loudly, though: the
        setting defaults False and ships commented out, so a silent gate would
        make --triage a no-op on a default install.
        """
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=False, auto_triage=False),
        )

        result = import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}), auto_triage=True)

        assert result.imported == 1
        assert result.queued_triage == 0
        assert WorkerCommand.objects.count() == 0
        assert "security_triage.enabled is false" in result.triage_skipped_reason

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_bad_config_fails_closed_with_a_reason(self, mock_config: MagicMock) -> None:
        """Can't read the config, can't know whether triage was switched off."""
        mock_config.side_effect = ValueError("operator.yaml: bad indent on line 12")

        result = import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}), auto_triage=True)

        assert result.imported == 1
        assert result.queued_triage == 0
        assert "could not read the operator config" in result.triage_skipped_reason
        assert "bad indent on line 12" in result.triage_skipped_reason

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_a_queueing_failure_still_leaves_the_report_imported(
        self, mock_config: MagicMock
    ) -> None:
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, auto_triage=True),
        )

        with patch(
            "franktheunicorn.security.queue.queue_triage", side_effect=RuntimeError("db is sad")
        ):
            result = import_reports_from_zip(make_zip({"a.txt": REPORT_TEXT}), auto_triage=True)

        assert result.imported == 1
        assert result.queued_triage == 0
        assert SecurityReport.objects.count() == 1
