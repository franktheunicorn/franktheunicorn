"""Tests for the shared-spreadsheet round trip (security.sheet_sync).

The property that matters here isn't "a CSV comes out" — it's that a sheet which
has been round-tripped through Google Sheets, edited by somebody who isn't the
operator, and imported a week later cannot silently destroy a ruling made in the
meantime. Most of these tests are about that.
"""

from __future__ import annotations

import csv
import io

import pytest

from franktheunicorn.core.models import SecurityReport
from franktheunicorn.security.sheet_sync import (
    CHECK_COLUMN,
    KEY_COLUMN,
    MAX_CELL_CHARS,
    TRUNCATION_MARKER,
    WRITABLE_COLUMNS,
    SheetImportResult,
    export_reports_csv,
    import_reports_csv,
    report_fingerprint,
    stream_reports_csv,
)
from tests.factories import ProjectFactory, SecurityReportFactory


def _export(reports: list[SecurityReport], *, full: bool = False) -> str:
    out = io.StringIO()
    export_reports_csv(reports, out, full=full)
    return out.getvalue()


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def _apply(text: str, **kwargs: bool) -> SheetImportResult:
    """Import through the same door the dashboard uses: the whole file as a string.

    Not ``text.splitlines()``. That deletes the newline inside a quoted
    multi-line cell and joins the words either side of it — ``"one\\ntwo"`` reads
    back as ``onetwo`` — which was live in the upload view until these tests
    caught it. Every test goes the way the view goes so it can't come back.
    """
    return import_reports_csv(text, **kwargs)


def _edit(text: str, report_id: int, column: str, value: str) -> str:
    """Rewrite one cell, the way a reviewer would in the sheet."""
    rows = _rows(text)
    header = list(_rows(text)[0].keys()) if rows else []
    for row in rows:
        if row[KEY_COLUMN] == str(report_id):
            row[column] = value
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


@pytest.mark.django_db
class TestExport:
    def test_header_and_one_row_per_report(self) -> None:
        reports = [SecurityReportFactory(title="One"), SecurityReportFactory(title="Two")]
        rows = _rows(_export(reports))
        assert len(rows) == 2
        assert {row["title"] for row in rows} == {"One", "Two"}
        for column in WRITABLE_COLUMNS:
            assert column in rows[0]

    def test_carries_the_check_token_and_the_id(self) -> None:
        report = SecurityReportFactory(status="triaging", operator_notes="mine")
        row = _rows(_export([report]))[0]
        assert row[KEY_COLUMN] == str(report.pk)
        assert row[CHECK_COLUMN] == report_fingerprint(report)

    def test_check_token_is_not_bare_digits(self) -> None:
        """A spreadsheet reformats an all-digit string and can lose leading zeros,
        which would disarm the staleness guard on whichever rows it happened to."""
        report = SecurityReportFactory()
        assert report_fingerprint(report).startswith("fp")
        assert not report_fingerprint(report).isdigit()

    def test_check_token_ignores_fields_the_import_cannot_write(self) -> None:
        """The worker finishing a triage run is not a conflict with a PMC comment."""
        report = SecurityReportFactory(triage_summary="before")
        before = report_fingerprint(report)
        report.triage_summary = "after — the worker got there"
        report.priority = 99.0
        report.save()
        assert report_fingerprint(report) == before

    def test_two_reports_in_the_same_state_get_different_tokens(self) -> None:
        """The token is a row-integrity check as well as a staleness one. Most rows
        are new/unknown with no notes, so without the id in the digest a sheet whose
        rows got sorted without the key column would apply one finding's ruling to
        another and the guard would wave it through."""
        first = SecurityReportFactory(status="new", assessed_severity="high")
        second = SecurityReportFactory(status="new", assessed_severity="high")

        assert report_fingerprint(first) != report_fingerprint(second)

    def test_check_token_changes_when_a_writable_field_does(self) -> None:
        report = SecurityReportFactory(status="new")
        before = report_fingerprint(report)
        report.status = "valid"
        report.save()
        assert report_fingerprint(report) != before

    def test_escapes_a_formula_in_an_attacker_supplied_title(self) -> None:
        """Report titles come from whoever filed the report, and this CSV is about
        to be opened in a spreadsheet by a PMC."""
        report = SecurityReportFactory(title='=HYPERLINK("https://evil.example","click")')
        row = _rows(_export([report]))[0]
        assert row["title"].startswith("'=HYPERLINK")

    @pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
    def test_escapes_every_formula_trigger(self, trigger: str) -> None:
        report = SecurityReportFactory(title=f"{trigger}cmd|' /c calc'!A0")
        assert _rows(_export([report]))[0]["title"].startswith(f"'{trigger}")

    def test_leaves_numeric_columns_alone_so_the_sheet_can_sort(self) -> None:
        report = SecurityReportFactory(priority=61.0)
        row = _rows(_export([report]))[0]
        assert row["priority"] == "61"
        assert row[KEY_COLUMN] == str(report.pk)

    def test_truncates_long_context_with_a_visible_marker(self) -> None:
        report = SecurityReportFactory(triage_summary="x" * (MAX_CELL_CHARS + 500))
        row = _rows(_export([report]))[0]
        assert row["triage_summary"].endswith(TRUNCATION_MARKER.strip())
        assert len(row["triage_summary"]) < MAX_CELL_CHARS + len(TRUNCATION_MARKER) + 2

    def test_default_export_leaves_out_the_payload(self) -> None:
        report = SecurityReportFactory(raw_text="the poc", proposed_patch="--- a/x")
        row = _rows(_export([report]))[0]
        assert "raw_text" not in row
        assert "proposed_patch" not in row

    def test_full_export_includes_it(self) -> None:
        report = SecurityReportFactory(raw_text="the poc", proposed_patch="--- a/x")
        row = _rows(_export([report], full=True))[0]
        assert row["raw_text"] == "the poc"
        # Escaped, because a unified diff opens "--- a/x" and a leading "-" is a
        # formula trigger in Excel. Sheets renders the apostrophe as the
        # literal-text marker, so the reviewer still reads "--- a/x", and the
        # column is read-only so nothing has to strip it back off.
        assert row["proposed_patch"] == "'--- a/x"

    def test_multiline_notes_survive_the_round_trip(self) -> None:
        report = SecurityReportFactory(operator_notes="line one\nline two")
        row = _rows(_export([report]))[0]
        assert row["operator_notes"] == "line one\nline two"

    def test_project_column_is_blank_for_an_unattached_report(self) -> None:
        report = SecurityReportFactory(project=None)
        assert _rows(_export([report]))[0]["project"] == ""

    def test_streaming_produces_the_same_bytes(self) -> None:
        reports = [SecurityReportFactory() for _ in range(3)]
        assert "".join(stream_reports_csv(reports)) == _export(reports)

    def test_the_machine_context_columns_are_there_for_the_reviewer(self) -> None:
        """The export audit: a reviewer deciding valid/invalid in the sheet needs
        what the machine already thinks — the staged verdict, the POC read, the
        sandbox, the CVE candidates, and any dup link — or they're re-deriving in
        a spreadsheet what the dashboard already knows."""
        twin = SecurityReportFactory()
        report = SecurityReportFactory(
            auto_triage_status="invalid",
            poc_plausible=False,
            is_expected_behavior=True,
            sandbox_verdict="reproduced",
            cve_matches=[{"cve_id": "CVE-2026-1234", "summary": "..."}, {"nope": True}],
            duplicate_of=twin,
            affected_versions="3.5.0 to 4.0.0",
            introduced_summary="Since SPARK-12345 in 3.5.0.",
            reporter_name="A. Researcher",
        )

        row = _rows(_export([report]))[0]

        assert row["auto_triage_suggestion"] == "invalid"
        assert row["poc_plausible"] == "no"
        assert row["expected_behavior"] == "yes"
        assert row["sandbox_verdict"] == "reproduced"
        assert row["cve_candidates"] == "CVE-2026-1234"
        assert row["duplicate_of_report"] == str(twin.pk)
        assert row["affected_versions"] == "3.5.0 to 4.0.0"
        assert row["introduced"] == "Since SPARK-12345 in 3.5.0."
        assert row["reporter"] == "A. Researcher"

    def test_the_new_columns_blank_out_cleanly_on_a_fresh_report(self) -> None:
        report = SecurityReportFactory()
        row = _rows(_export([report]))[0]
        for column in (
            "auto_triage_suggestion",
            "poc_plausible",
            "expected_behavior",
            "sandbox_verdict",
            "cve_candidates",
            "duplicate_of_report",
            "affected_versions",
            "introduced",
            "reporter",
        ):
            assert row[column] == "", column

    def test_poc_plausible_distinguishes_yes_no_and_unrun(self) -> None:
        """None means the POC assessor never ran; "no" is a verdict. Collapsing
        those into one cell would tell the reviewer a POC was implausible when
        nobody looked."""
        rows = _rows(
            _export(
                [
                    SecurityReportFactory(poc_plausible=True),
                    SecurityReportFactory(poc_plausible=False),
                    SecurityReportFactory(poc_plausible=None),
                ]
            )
        )
        assert [row["poc_plausible"] for row in rows] == ["yes", "no", ""]

    def test_the_new_columns_are_read_only_on_import(self) -> None:
        """They're context for the reviewer, not a back door into fields the import
        doesn't own — an edited auto_triage_suggestion must not write."""
        report = SecurityReportFactory(status="new", auto_triage_status="invalid")
        text = _edit(_export([report]), report.pk, "auto_triage_suggestion", "valid")
        text = _edit(text, report.pk, "sandbox_verdict", "not-reproduced")

        result = _apply(text)

        assert result.unchanged == 1
        report.refresh_from_db()
        assert report.auto_triage_status == "invalid"
        assert report.sandbox_verdict == ""


@pytest.mark.django_db
class TestImportApplies:
    def test_applies_an_edited_status(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "valid")

        result = _apply(text)

        assert result.applied == 1
        report.refresh_from_db()
        assert report.status == "valid"

    def test_accepts_the_label_a_reviewer_would_actually_type(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "Expected Behavior")

        assert _apply(text).applied == 1
        report.refresh_from_db()
        assert report.status == "expected-behavior"

    def test_lands_a_pmc_comment_in_external_notes_with_a_timestamp(self) -> None:
        report = SecurityReportFactory(operator_notes="my own note")
        text = _edit(_export([report]), report.pk, "external_notes", "PMC: agreed, ship it")

        _apply(text)

        report.refresh_from_db()
        assert report.external_notes == "PMC: agreed, ship it"
        assert report.external_notes_at is not None
        # Not merged into the operator's own field: the next export has to be able
        # to tell whose text it is sending back out.
        assert report.operator_notes == "my own note"

    def test_does_not_restamp_external_notes_on_an_unedited_reimport(self) -> None:
        report = SecurityReportFactory()
        text = _edit(_export([report]), report.pk, "external_notes", "PMC: looks real")
        _apply(text)
        report.refresh_from_db()
        first = report.external_notes_at

        # Same sheet, imported again — and it has to be re-exported first, because
        # the check token moved when the note landed.
        again = _edit(_export([report]), report.pk, "external_notes", "PMC: looks real")
        result = _apply(again)

        assert result.unchanged == 1
        report.refresh_from_db()
        assert report.external_notes_at == first

    def test_normalises_a_cve_and_rejects_a_non_cve(self) -> None:
        report = SecurityReportFactory(status="new")
        good = _edit(_export([report]), report.pk, "duplicate_of_cve", "cve-2026-1234")
        assert _apply(good).applied == 1
        report.refresh_from_db()
        assert report.matched_cve_id == "CVE-2026-1234"

        bad = _edit(_export([report]), report.pk, "duplicate_of_cve", "probably CVE-ish")
        result = _apply(bad)
        assert result.applied == 0
        assert result.failed == 1
        assert "isn't a CVE id" in result.rows[0].detail

    def test_flags_duplicate_with_no_cve_without_refusing_it(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "duplicate")

        result = _apply(text)

        assert result.applied == 1
        assert "no CVE" in result.rows[0].detail

    def test_a_formula_leading_note_round_trips_without_drifting(self) -> None:
        """A stored note opening with a formula trigger goes out escaped. Coming
        back untouched it has to read as unchanged, or the text drifts a character
        on every import and external_notes_at restamps for no reason."""
        report = SecurityReportFactory(external_notes="=1+1 looks exploitable")
        text = _export([report])
        assert _rows(text)[0]["external_notes"] == "'=1+1 looks exploitable"

        result = _apply(text)

        assert result.unchanged == 1
        report.refresh_from_db()
        assert report.external_notes == "=1+1 looks exploitable"

    def test_a_note_that_really_starts_with_an_apostrophe_keeps_it(self) -> None:
        """The bug that replaced a strip-the-leading-apostrophe rule: a note like
        ``'--force' is not the answer here`` is character-for-character the same as
        an escaped ``--force``, and stripping ate the reviewer's quote mark."""
        report = SecurityReportFactory()
        text = _edit(
            _export([report]), report.pk, "external_notes", "'--force' is not the answer here"
        )

        _apply(text)

        report.refresh_from_db()
        assert report.external_notes == "'--force' is not the answer here"

    def test_a_note_starting_with_a_blank_line_survives_untouched(self) -> None:
        """CR is a formula trigger, so a note beginning with a blank line — what a
        textarea gives you — went out escaped as "'\\r\\nfoo" and came back folded to
        "'\\nfoo", matching none of the compared forms. It applied: blank line gone,
        apostrophe promoted to line one, and the rewrite burned the check token so
        re-importing the same sheet then conflicted."""
        report = SecurityReportFactory(operator_notes="\r\nfoo bar")

        result = _apply(_export([report]))

        assert result.unchanged == 1
        report.refresh_from_db()
        assert report.operator_notes == "\r\nfoo bar"

    def test_a_note_too_long_for_a_cell_is_not_written_back_blank(self) -> None:
        """Truncating a writable column is only safe if the truncated value is never
        written back — otherwise an untouched round trip deletes the tail. And the
        refusal has to be audible, because if the reviewer edited the visible part,
        this is where their edit stops."""
        from franktheunicorn.security.sheet_sync import MAX_NOTE_CHARS

        long_note = "x" * (MAX_NOTE_CHARS + 500)
        report = SecurityReportFactory(operator_notes=long_note)
        text = _export([report])
        assert TRUNCATION_MARKER.strip() in _rows(text)[0]["operator_notes"]

        result = _apply(text)

        report.refresh_from_db()
        assert report.operator_notes == long_note
        assert "left as it was" in result.rows[0].detail
        assert result.rows[0].needs_attention

    def test_an_export_of_a_huge_note_is_still_importable(self) -> None:
        """The point of bounding the note columns: unbounded, a note past csv's field
        limit produced a file this module's own importer refused in its entirety."""
        report = SecurityReportFactory(operator_notes="y" * 200_000, status="new")
        text = _edit(_export([report]), report.pk, "status", "valid")

        result = _apply(text)

        assert result.error == ""
        report.refresh_from_db()
        assert report.status == "valid"

    def test_strips_control_characters_but_keeps_tabs_and_newlines(self) -> None:
        """SQLite stores a NUL in a text column happily; Postgres refuses it, and
        this codebase claims to work on both."""
        report = SecurityReportFactory()
        text = _edit(
            _export([report]), report.pk, "external_notes", "before\x00after\tcol\nline two\x07"
        )

        _apply(text)

        report.refresh_from_db()
        assert report.external_notes == "beforeafter\tcol\nline two"

    def test_keeps_an_apostrophe_that_is_part_of_the_note(self) -> None:
        report = SecurityReportFactory()
        text = _edit(_export([report]), report.pk, "external_notes", "'tis fine")
        _apply(text)
        report.refresh_from_db()
        assert report.external_notes == "'tis fine"

    def test_windows_line_endings_are_not_an_edit(self) -> None:
        report = SecurityReportFactory(operator_notes="one\ntwo")
        text = _edit(_export([report]), report.pk, "operator_notes", "one\r\ntwo")

        result = _apply(text)

        assert result.unchanged == 1
        assert result.applied == 0

    def test_a_note_the_dashboard_saved_is_not_a_phantom_edit(self) -> None:
        """Browsers submit a textarea with CRLF line endings, per the HTML spec, so
        every note the operator saved through the dashboard has them. Importing an
        untouched sheet used to report "applied 1" and rewrite the stored text —
        phantom churn on a row nobody had touched."""
        report = SecurityReportFactory(operator_notes="line one\r\nline two")

        result = _apply(_export([report]))

        assert result.unchanged == 1
        report.refresh_from_db()
        # Left exactly as the dashboard stored it. An import is not the place to
        # normalise line endings behind the operator's back.
        assert report.operator_notes == "line one\r\nline two"

    def test_reports_every_changed_field_for_the_audit_line(self) -> None:
        report = SecurityReportFactory(status="new", assessed_severity="unknown")
        text = _edit(_export([report]), report.pk, "status", "valid")
        text = _edit(text, report.pk, "severity", "high")

        result = _apply(text)

        assert result.rows[0].changed == ("assessed_severity", "status")


@pytest.mark.django_db
class TestImportRefuses:
    def test_refuses_a_row_whose_report_changed_after_the_export(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "invalid")

        # The operator rules on it in the dashboard while the sheet is out.
        report.status = "valid"
        report.operator_notes = "confirmed on main"
        report.save()

        result = _apply(text)

        assert result.conflicts == 1
        assert result.applied == 0
        report.refresh_from_db()
        assert report.status == "valid"
        assert report.operator_notes == "confirmed on main"

    def test_names_the_conflicting_fields_so_the_operator_can_judge(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "invalid")
        report.status = "valid"
        report.save()

        result = _apply(text)

        assert result.rows[0].changed == ("status",)
        assert "after the export" in result.rows[0].detail

    def test_force_lets_the_sheet_win_and_says_so(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "invalid")
        report.status = "valid"
        report.save()

        result = _apply(text, force=True)

        assert result.applied == 1
        assert "forced" in result.rows[0].detail
        report.refresh_from_db()
        assert report.status == "invalid"

    def test_a_month_old_sheet_nobody_edited_changes_nothing(self) -> None:
        """The guard is about clobbering, not about age. A sheet the reviewer never
        touched has nothing to clobber, however stale the rest of the row is."""
        report = SecurityReportFactory(status="new")
        text = _export([report])
        # The worker triages in the meantime. Nothing the import can write.
        report.triage_summary = "the worker ran since"
        report.priority = 88.0
        report.save()

        result = _apply(text)

        assert result.conflicts == 0
        assert result.unchanged == 1

    def test_an_untouched_row_that_would_revert_a_ruling_is_a_conflict(self) -> None:
        """The case the guard exists for, and the one that needs no bad behaviour
        from anybody: the sheet goes out, the operator rules on the report in the
        dashboard, and the unedited sheet still carries the old status."""
        report = SecurityReportFactory(status="new")
        text = _export([report])
        report.status = "valid"
        report.save()

        result = _apply(text)

        assert result.conflicts == 1
        report.refresh_from_db()
        assert report.status == "valid"

    def test_a_deleted_column_does_not_blank_the_field(self) -> None:
        report = SecurityReportFactory(operator_notes="keep me", status="new")
        rows = _rows(_export([report]))
        header = [column for column in rows[0] if column != "operator_notes"]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row["status"] = "valid"
            writer.writerow(row)

        result = _apply(out.getvalue())

        assert result.applied == 1
        report.refresh_from_db()
        assert report.status == "valid"
        assert report.operator_notes == "keep me"

    def test_a_short_row_does_not_blank_the_cells_it_stops_before(self) -> None:
        """A real spreadsheet export drops trailing empty columns, so the row ends
        early and DictReader fills the rest with None. Reading that as "" means an
        ordinary download wipes every note in the backlog."""
        report = SecurityReportFactory(operator_notes="keep me", external_notes="and me")
        text = _export([report])
        header, row = text.splitlines()[:2]
        # Truncate the row after the status column, the way a sheet with empty
        # trailing cells comes out of some tools.
        keep = header.split(",").index("status") + 1
        short = ",".join(row.split(",")[:keep])

        result = _apply(f"{header}\n{short}\n")

        assert result.unchanged == 1
        report.refresh_from_db()
        assert report.operator_notes == "keep me"
        assert report.external_notes == "and me"

    def test_extra_cells_past_the_header_are_ignored_not_fatal(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "valid")
        header, row = text.splitlines()[:2]

        result = _apply(f"{header}\n{row},somebody's extra column\n")

        assert result.applied == 1
        report.refresh_from_db()
        assert report.status == "valid"

    def test_refuses_more_rows_than_the_cap_without_reading_them_all(self) -> None:
        from franktheunicorn.security.sheet_sync import MAX_IMPORT_ROWS

        report = SecurityReportFactory()
        header = _export([report]).splitlines()[0]
        row = f"{report.pk},fpdeadbeef01," + "," * (len(header.split(",")) - 3)

        def lines() -> object:
            yield header
            for _ in range(MAX_IMPORT_ROWS + 10):
                yield row
            raise AssertionError("the importer read past the cap")

        result = import_reports_csv(lines())  # type: ignore[arg-type]

        assert str(MAX_IMPORT_ROWS) in result.error

    def test_an_explicitly_emptied_cell_does_clear_the_field(self) -> None:
        report = SecurityReportFactory(operator_notes="delete me")
        text = _edit(_export([report]), report.pk, "operator_notes", "")

        assert _apply(text).applied == 1
        report.refresh_from_db()
        assert report.operator_notes == ""

    def test_rejects_an_unknown_status(self) -> None:
        report = SecurityReportFactory()
        text = _edit(_export([report]), report.pk, "status", "probably fine tbh")

        result = _apply(text)

        assert result.failed == 1
        assert "isn't one of" in result.rows[0].detail
        report.refresh_from_db()
        assert report.status == "new"

    def test_rejects_an_unknown_severity(self) -> None:
        report = SecurityReportFactory(assessed_severity="unknown")
        text = _edit(_export([report]), report.pk, "severity", "quite bad")

        result = _apply(text)

        assert result.failed == 1
        assert "isn't one of" in result.rows[0].detail
        report.refresh_from_db()
        assert report.assessed_severity == "unknown"

    def test_an_impossible_id_is_rejected_before_it_reaches_the_database(self) -> None:
        """Twenty digits parses as a Python int and then raises OverflowError out of
        the pk__in query — an unhandled 500 on the upload endpoint."""
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, KEY_COLUMN, "9" * 20)

        result = _apply(text)

        assert result.rows[0].outcome == "no-id"
        assert "isn't a possible id" in result.rows[0].detail
        report.refresh_from_db()
        assert report.status == "new"

    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_a_nonsense_id_is_rejected_too(self, bad: str) -> None:
        report = SecurityReportFactory()
        text = _edit(_export([report]), report.pk, KEY_COLUMN, bad)
        assert _apply(text).rows[0].outcome == "no-id"

    def test_an_overlong_cve_is_rejected_rather_than_stored_past_max_length(self) -> None:
        """SQLite doesn't enforce max_length, so a 209-character "CVE" went into a
        CharField(max_length=50) without complaint — and would have exploded on the
        Postgres install the docs promise works."""
        report = SecurityReportFactory()
        text = _edit(_export([report]), report.pk, "duplicate_of_cve", "CVE-2026-" + "1" * 200)

        result = _apply(text)

        assert result.failed == 1
        assert "isn't a CVE id" in result.rows[0].detail
        report.refresh_from_db()
        assert report.matched_cve_id == ""

    def test_a_real_cve_still_passes(self) -> None:
        report = SecurityReportFactory()
        text = _edit(_export([report]), report.pk, "duplicate_of_cve", "CVE-2026-1234567")
        assert _apply(text).applied == 1

    def test_a_non_numeric_id_is_reported_not_a_traceback(self) -> None:
        """Somebody sorts the sheet with the header inside the range, or pastes a
        label into the key column."""
        report = SecurityReportFactory()
        text = _edit(_export([report]), report.pk, KEY_COLUMN, "report_id")

        result = _apply(text)

        assert result.rows[0].outcome == "no-id"
        assert "isn't a number" in result.rows[0].detail

    def test_a_malformed_csv_is_an_error_not_a_crash(self) -> None:
        """A cell past csv's own field limit raises out of the reader mid-file."""
        report = SecurityReportFactory()
        text = _export([report])
        header = text.splitlines()[0]
        giant = f'{report.pk},fpdeadbeef01,,0,"{"x" * (csv.field_size_limit() + 10)}'

        result = _apply(f"{header}\n{giant}\n")

        assert "could not read the CSV" in result.error

    def test_an_oversized_cell_names_the_row_and_applies_nothing(self) -> None:
        """One pasted stack trace took the whole import down with a message that
        didn't say which row, leaving a few hundred cells to search by eye. The
        all-or-nothing part is deliberate — the reader's position isn't trustworthy
        after a field blows up, so applying the earlier half would commit an
        arbitrary prefix of somebody's review."""
        innocent = SecurityReportFactory(status="new")
        broken = SecurityReportFactory(status="new")
        text = _edit(_export([innocent, broken]), innocent.pk, "status", "valid")
        header, *body = text.splitlines()
        # Row 3 of the sheet (header is row 1) gets the oversized cell.
        oversized = f'{broken.pk},fpdeadbeef01,,0,"{"x" * (csv.field_size_limit() + 10)}'

        result = _apply(f"{header}\n{body[0]}\n{oversized}\n")

        assert "row 3" in result.error
        assert "too big" in result.error
        assert result.applied == 0
        innocent.refresh_from_db()
        assert innocent.status == "new"

    def test_a_hand_added_row_with_no_id_is_reported_not_silently_dropped(self) -> None:
        report = SecurityReportFactory()
        text = _export([report])
        blank = "," * (len(_rows(text)[0]) - 1)
        result = _apply(text + blank + "\n")

        assert result.failed == 1
        assert result.rows[-1].outcome == "no-id"

    def test_an_id_from_another_install_is_reported(self) -> None:
        report = SecurityReportFactory()
        text = _edit(_export([report]), report.pk, KEY_COLUMN, "999999")

        result = _apply(text)

        assert result.rows[0].outcome == "unknown-report"

    def test_a_duplicated_row_does_not_get_applied_twice(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "valid")
        lines = text.splitlines()
        doubled = [*lines, lines[-1]]

        result = import_reports_csv(doubled)

        assert result.applied == 1
        assert result.rows[1].outcome == "duplicate-row"

    def test_refuses_a_file_that_is_not_an_export_from_here(self) -> None:
        result = import_reports_csv(["name,severity", "some scan,high"])
        assert result.error
        assert KEY_COLUMN in result.error

    def test_refuses_a_file_with_no_editable_columns(self) -> None:
        result = import_reports_csv([f"{KEY_COLUMN},title", "1,a thing"])
        assert "none of the editable columns" in result.error

    def test_refuses_an_empty_file(self) -> None:
        assert import_reports_csv([]).error == "the file is empty"

    def test_a_blank_check_cell_says_it_was_applied_blind(self) -> None:
        """The column being there isn't the same as the cell being filled. A row
        whose token got cleared gets the same write as a good one with none of the
        confidence, and "applied 1" alone reads as "checked"."""
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "valid")
        text = _edit(text, report.pk, CHECK_COLUMN, "")

        result = _apply(text)

        assert result.applied == 1
        assert result.unguarded == 1
        assert "unchecked" in result.summary()
        assert "applied" in result.summary() and "blind" in result.summary()

    def test_warns_loudly_when_the_check_column_was_deleted(self) -> None:
        report = SecurityReportFactory(status="new")
        rows = _rows(_export([report]))
        header = [column for column in rows[0] if column != CHECK_COLUMN]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row["status"] = "valid"
            writer.writerow(row)

        result = _apply(out.getvalue())

        assert result.applied == 1
        assert result.unguarded == 1
        assert any("applied blind" in warning for warning in result.warnings)


@pytest.mark.django_db
class TestDryRun:
    def test_reports_what_it_would_do_and_writes_nothing(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "valid")

        result = _apply(text, dry_run=True)

        assert result.applied == 1
        assert result.dry_run
        assert "would apply" in result.summary()
        report.refresh_from_db()
        assert report.status == "new"

    def test_dry_run_still_finds_the_conflicts(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "invalid")
        report.status = "valid"
        report.save()

        result = _apply(text, dry_run=True)

        assert result.conflicts == 1


@pytest.mark.django_db
class TestSummary:
    def test_points_at_the_way_out_of_a_conflict(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "invalid")
        report.status = "valid"
        report.save()

        summary = _apply(text).summary()

        assert "conflicted" in summary
        # Door-neutral wording. This string is rendered verbatim into the
        # dashboard's flash message, where there is no --force — the control is a
        # checkbox. The CLI names the flag itself.
        assert "re-import letting the sheet win" in summary
        assert "--force" not in summary

    def test_a_clean_run_does_not_mention_conflicts(self) -> None:
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "valid")
        assert "conflict" not in _apply(text).summary()

    def test_a_forced_overwrite_is_counted_not_just_detailed(self) -> None:
        """The one outcome here that destroys work somebody already did, and the
        summary was reporting it as an ordinary "applied 1"."""
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "invalid")
        report.status = "valid"
        report.save()

        result = _apply(text, force=True)

        assert result.forced == 1
        assert "forced over newer work" in result.summary()
        # And it has to reach the operator through the per-row channel too, which
        # filtered on outcome alone and so skipped every applied row.
        assert result.rows[0].needs_attention


class TestCleaners:
    """The two normalisers both write paths share, so a column cleaned on one of
    them isn't a column cleaned on neither."""

    def test_single_line_collapses_whitespace(self) -> None:
        from franktheunicorn.security.sheet_sync import clean_single_line

        assert clean_single_line("  master \n branch-3.5\t ") == "master branch-3.5"

    def test_single_line_drops_invisibles_a_c0_regex_misses(self) -> None:
        """ZWSP, BOM and the bidi overrides are category Cf: not isspace(), so they
        survive .strip() and a [\\x00-\\x1f] regex both."""
        from franktheunicorn.security.sheet_sync import clean_single_line

        assert clean_single_line("mas\u200bter\ufeff-3.5\x00") == "master-3.5"
        assert clean_single_line("\u200b\ufeff\u202e") == ""

    def test_multi_line_keeps_the_lines(self) -> None:
        from franktheunicorn.security.sheet_sync import clean_multi_line

        assert clean_multi_line("4.0\r\n3.5\x00\r3.4") == "4.0\n3.5\n3.4"


@pytest.mark.django_db
class TestSelection:
    def test_export_selection_is_ranked_and_filterable(self) -> None:
        from franktheunicorn.security.sheet_sync import reports_for_export

        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, priority=1.0, status="new", title="low")
        SecurityReportFactory(project=project, priority=90.0, status="new", title="high")
        SecurityReportFactory(project=project, priority=50.0, status="valid", title="ruled")
        SecurityReportFactory(priority=99.0, status="new", title="other project")

        ranked = list(reports_for_export(project="apache/spark"))
        assert [report.title for report in ranked] == ["high", "ruled", "low"]

        new_only = list(reports_for_export(project="apache/spark", status="new"))
        assert [report.title for report in new_only] == ["high", "low"]

        assert len(list(reports_for_export(limit=2))) == 2

    def test_an_edit_to_the_read_only_branch_column_is_reported_not_dropped(self) -> None:
        """The CVE-no-branch export hands a PMC rows defined by having no branch, so
        that column is the one they answer in — and a discard nobody is told about is
        worse than a column that isn't there."""
        import io

        from franktheunicorn.security.sheet_sync import export_reports_csv, import_reports_csv

        report = SecurityReportFactory(status="valid", fixed_in_branch="")
        out = io.StringIO()
        export_reports_csv(SecurityReport.objects.all(), out)
        rows = out.getvalue().splitlines()
        col = rows[0].split(",").index("fixed_in_branch")
        cells = rows[1].split(",")
        cells[col] = "branch-3.5"
        edited = rows[0] + "\n" + ",".join(cells) + "\n"

        result = import_reports_csv(edited)

        report.refresh_from_db()
        assert report.fixed_in_branch == ""
        outcome = result.rows[0]
        assert "fixed_in_branch is read-only" in outcome.detail
        # An unchanged row with something to say still reaches the operator.
        assert outcome.needs_attention

    def test_the_sheet_shows_the_fix_branch(self) -> None:
        """The export can be filtered to the "no branch recorded" queue, so the
        reviewer needs a column showing the branch to check that premise."""
        import io

        from franktheunicorn.security.sheet_sync import export_reports_csv

        SecurityReportFactory(status="valid", fixed_in_branch="branch-3.5")

        out = io.StringIO()
        export_reports_csv(SecurityReport.objects.all(), out)
        text = out.getvalue()

        assert "fixed_in_branch" in text.splitlines()[0]
        assert "branch-3.5" in text

    def test_export_selection_understands_the_cve_without_branch_filter(self) -> None:
        """Not a status, but the list page offers it as a tab and the export's
        contract is "export what I'm looking at"."""
        from franktheunicorn.security.sheet_sync import reports_for_export

        SecurityReportFactory(status="valid", matched_cve_id="CVE-2026-1111", title="unfixed")
        SecurityReportFactory(
            status="valid",
            matched_cve_id="CVE-2026-2222",
            fixed_in_branch="branch-3.5",
            title="fixed",
        )
        SecurityReportFactory(status="valid", title="no cve")
        SecurityReportFactory(status="duplicate", matched_cve_id="CVE-2026-3333", title="dup")

        selected = reports_for_export(status=SecurityReport.CVE_NO_BRANCH_FILTER)

        assert [report.title for report in selected] == ["unfixed"]

    def test_export_honours_the_newest_sort(self) -> None:
        """A trickle of emailed reports all rank 0.0, so arrival order is the right
        one for an inbox — and an export that silently re-ranks means the row cap
        keeps a different set than the operator was looking at."""
        from franktheunicorn.security.sheet_sync import reports_for_export

        SecurityReportFactory(title="ranked high", priority=90.0)
        SecurityReportFactory(title="arrived later", priority=1.0)

        by_priority = [r.title for r in reports_for_export(sort="priority")]
        by_arrival = [r.title for r in reports_for_export(sort="newest")]

        assert by_priority == ["ranked high", "arrived later"]
        assert by_arrival == ["arrived later", "ranked high"]
        # An unknown sort falls back rather than erroring, matching the list view.
        assert [r.title for r in reports_for_export(sort="; DROP TABLE")] == by_priority

    def test_a_zero_limit_exports_nothing_rather_than_everything(self) -> None:
        """`if limit:` fell through to an unsliced queryset. Of all the functions to
        fail open, one that writes unfixed vulnerability reports to a shared sheet is
        the wrong one."""
        from franktheunicorn.security.sheet_sync import reports_for_export

        SecurityReportFactory()
        SecurityReportFactory()

        assert list(reports_for_export(limit=0)) == []
        assert len(list(reports_for_export())) == 2

    def test_the_filename_says_which_slice_it_is(self) -> None:
        """Two differently-scoped exports on one day were both
        security-review-<date>.csv, so the browser named the second "(1)" and
        nothing in either file said which slice of the backlog it held."""
        from franktheunicorn.security.sheet_sync import export_filename

        assert "-new-" in export_filename(status="new")
        assert "-full" in export_filename(full=True)
        assert "-new-" not in export_filename()


@pytest.mark.django_db
class TestForcedCountIsNotDerivedFromProse:
    def test_rewording_the_detail_cannot_zero_the_count(self) -> None:
        """It was re-derived with detail.startswith("forced"), so rewording an
        operator-facing message silently zeroed the tally of rows that destroyed
        somebody's work. Counted where it happens now."""
        report = SecurityReportFactory(status="new")
        text = _edit(_export([report]), report.pk, "status", "invalid")
        report.status = "valid"
        report.save()

        result = _apply(text, force=True)

        assert result.forced == 1
        assert result.forced_count == 1
        # Independent of the wording: blanking the detail must not change the count.
        object.__setattr__(result.rows[0], "detail", "")
        assert result.forced == 1
