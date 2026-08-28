"""Tests for the export_security_csv / import_security_csv management commands.

The shell door onto the same round trip the dashboard offers, for a backlog too
big to push through a browser or a box where the dashboard isn't reachable.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from tests.factories import ProjectFactory, SecurityReportFactory


@pytest.mark.django_db
class TestExportCommand:
    def test_writes_the_csv_to_stdout_by_default(self) -> None:
        SecurityReportFactory(title="Deserialization in the loader")
        out = io.StringIO()

        call_command("export_security_csv", stdout=out)

        body = out.getvalue()
        assert body.startswith("report_id,check,")
        assert "Deserialization in the loader" in body

    def test_the_count_goes_to_stderr_so_stdout_stays_a_valid_csv(self) -> None:
        """A summary line in the middle of the file makes it unimportable by its
        own importer."""
        SecurityReportFactory()
        out, err = io.StringIO(), io.StringIO()

        call_command("export_security_csv", stdout=out, stderr=err)

        assert "1 report(s) exported." in err.getvalue()
        assert "exported" not in out.getvalue()

    def test_writes_a_dated_file_when_out_is_given_bare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        SecurityReportFactory()
        out = io.StringIO()
        monkeypatch.chdir(tmp_path)

        call_command("export_security_csv", "--out", stdout=out)

        written = list(tmp_path.glob("security-review-*.csv"))
        assert len(written) == 1
        assert written[0].read_text().startswith("report_id,check,")
        # The instructions matter as much as the file: the round trip has a step
        # (keep the check column) that isn't guessable.
        assert "check" in out.getvalue()
        assert "import_security_csv" in out.getvalue()

    def test_writes_the_named_file(self, tmp_path: Path) -> None:
        SecurityReportFactory()
        target = tmp_path / "for-the-pmc.csv"

        call_command("export_security_csv", "--out", str(target), stdout=io.StringIO())

        assert target.read_text().startswith("report_id,check,")

    def test_filters_by_project_and_status(self, tmp_path: Path) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReportFactory(project=project, title="spark one", status="new")
        SecurityReportFactory(project=project, title="spark ruled", status="valid")
        SecurityReportFactory(title="somebody else's", status="new")
        target = tmp_path / "out.csv"

        call_command(
            "export_security_csv",
            "--project",
            "apache/spark",
            "--status",
            "new",
            "--out",
            str(target),
            stdout=io.StringIO(),
        )

        body = target.read_text()
        assert "spark one" in body
        assert "spark ruled" not in body
        assert "somebody else's" not in body

    def test_rejects_a_bad_status(self) -> None:
        with pytest.raises(CommandError, match="--status must be one of"):
            call_command("export_security_csv", "--status", "probably-fine", stdout=io.StringIO())

    def test_rejects_a_project_that_is_not_here(self) -> None:
        with pytest.raises(CommandError, match="No project"):
            call_command("export_security_csv", "--project", "nope/nope", stdout=io.StringIO())

    def test_rejects_a_malformed_project(self) -> None:
        with pytest.raises(CommandError, match="owner/repo"):
            call_command("export_security_csv", "--project", "spark", stdout=io.StringIO())

    def test_rejects_a_zero_limit(self) -> None:
        with pytest.raises(CommandError, match="at least 1"):
            call_command("export_security_csv", "--limit", "0", stdout=io.StringIO())


@pytest.mark.django_db
class TestImportCommand:
    def _exported(self, tmp_path: Path) -> Path:
        target = tmp_path / "review.csv"
        call_command("export_security_csv", "--out", str(target), stdout=io.StringIO())
        return target

    def test_applies_the_edits(self, tmp_path: Path) -> None:
        report = SecurityReportFactory(status="new")
        path = self._exported(tmp_path)
        path.write_text(path.read_text().replace(",new,", ",valid,"))
        out = io.StringIO()

        call_command("import_security_csv", str(path), stdout=out)

        report.refresh_from_db()
        assert report.status == "valid"
        assert "applied 1" in out.getvalue()

    def test_dry_run_writes_nothing_and_says_so(self, tmp_path: Path) -> None:
        report = SecurityReportFactory(status="new")
        path = self._exported(tmp_path)
        path.write_text(path.read_text().replace(",new,", ",valid,"))
        out = io.StringIO()

        call_command("import_security_csv", str(path), "--dry-run", stdout=out)

        report.refresh_from_db()
        assert report.status == "new"
        assert "would apply 1" in out.getvalue()
        assert "nothing was written" in out.getvalue()

    def test_names_the_conflicting_row_rather_than_reverting(self, tmp_path: Path) -> None:
        report = SecurityReportFactory(status="new")
        path = self._exported(tmp_path)
        path.write_text(path.read_text().replace(",new,", ",invalid,"))
        report.status = "valid"
        report.save()
        out = io.StringIO()

        call_command("import_security_csv", str(path), stdout=out)

        report.refresh_from_db()
        assert report.status == "valid"
        body = out.getvalue()
        assert "conflict" in body
        assert "row 2" in body
        assert "--force" in body

    def test_force_applies_it(self, tmp_path: Path) -> None:
        report = SecurityReportFactory(status="new")
        path = self._exported(tmp_path)
        path.write_text(path.read_text().replace(",new,", ",invalid,"))
        report.status = "valid"
        report.save()

        call_command("import_security_csv", str(path), "--force", stdout=io.StringIO())

        report.refresh_from_db()
        assert report.status == "invalid"

    def test_multiline_notes_survive_the_file_path(self, tmp_path: Path) -> None:
        """The CLI hands an open file to the importer; a quoted multi-line cell has
        to come back with its newline intact."""
        report = SecurityReportFactory()
        path = self._exported(tmp_path)
        header, row = path.read_text().splitlines()[:2]
        columns = header.split(",")
        cells = row.split(",")
        cells[columns.index("external_notes")] = '"first line\nsecond line"'
        path.write_text(f"{header}\n{','.join(cells)}\n")

        call_command("import_security_csv", str(path), stdout=io.StringIO())

        report.refresh_from_db()
        assert report.external_notes == "first line\nsecond line"

    def test_a_bom_from_a_real_download_is_not_a_broken_file(self, tmp_path: Path) -> None:
        report = SecurityReportFactory(status="new")
        path = self._exported(tmp_path)
        path.write_bytes(b"\xef\xbb\xbf" + path.read_text().replace(",new,", ",valid,").encode())

        call_command("import_security_csv", str(path), stdout=io.StringIO())

        report.refresh_from_db()
        assert report.status == "valid"

    def test_a_non_utf8_file_is_a_command_error_not_a_traceback(self, tmp_path: Path) -> None:
        """A UTF-16 download, or an .xlsx renamed to .csv. Used to surface as a bare
        UnicodeDecodeError from inside the csv reader, which reads as "the tool is
        broken" rather than "save it as CSV"."""
        path = tmp_path / "review.csv"
        path.write_bytes("report_id,status\n1,valid\n".encode("utf-16"))

        with pytest.raises(CommandError, match="isn't UTF-8"):
            call_command("import_security_csv", str(path), stdout=io.StringIO())

    def test_exits_non_zero_on_a_file_that_is_not_an_export(self, tmp_path: Path) -> None:
        path = tmp_path / "other.csv"
        path.write_text("finding,severity\nsomething,high\n")

        with pytest.raises(CommandError, match="report_id"):
            call_command("import_security_csv", str(path), stdout=io.StringIO())

    def test_missing_file_is_a_command_error_not_a_traceback(self, tmp_path: Path) -> None:
        with pytest.raises(CommandError, match="No such file"):
            call_command("import_security_csv", str(tmp_path / "nope.csv"), stdout=io.StringIO())

    def test_round_trips_a_full_export(self, tmp_path: Path) -> None:
        """--full adds two columns the importer doesn't write. It must not choke on
        them, and it must not try to write them back."""
        report = SecurityReportFactory(status="new", raw_text="the poc")
        target = tmp_path / "full.csv"
        call_command("export_security_csv", "--full", "--out", str(target), stdout=io.StringIO())
        target.write_text(target.read_text().replace(",new,", ",valid,"))

        call_command("import_security_csv", str(target), stdout=io.StringIO())

        report.refresh_from_db()
        assert report.status == "valid"
        assert report.raw_text == "the poc"
