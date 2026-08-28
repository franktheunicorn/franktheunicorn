"""Import a reviewed security CSV back onto the reports it was exported from."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.security.sheet_sync import import_reports_csv

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    help = (
        "Apply a reviewed CSV (from export_security_csv, edited in a shared "
        "spreadsheet) back onto the security reports it came from"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("path", help="The downloaded CSV")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report every row's outcome and write nothing",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Apply rows whose report changed after the export. Off by "
                "default, because the alternative to refusing is silently "
                "picking a winner between the sheet and a later ruling."
            ),
        )
        parser.add_argument(
            "--verbose-rows",
            action="store_true",
            help="List every row, not just the ones that needed attention",
        )

    def handle(self, *args: object, **options: object) -> None:
        path = Path(str(options["path"]))
        if not path.is_file():
            raise CommandError(f"No such file: {path}")

        # newline="" per the csv module: it does its own line handling, and
        # letting Python translate first splits a quoted multi-line note (which
        # is what every operator_notes cell is) across rows.
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                result = import_reports_csv(
                    handle,
                    dry_run=bool(options.get("dry_run")),
                    force=bool(options.get("force")),
                )
        except UnicodeDecodeError as exc:
            # A UTF-16 or Latin-1 download, or an .xlsx renamed to .csv. Used to
            # come out as a bare traceback from inside the csv reader, which
            # reads as "the tool is broken" rather than "save it as CSV".
            raise CommandError(
                f"{path.name} isn't UTF-8 text ({exc.reason}). Download the sheet "
                "as Comma-separated values, not as .xlsx, .ods or UTF-16."
            ) from exc

        show_all = bool(options.get("verbose_rows"))
        for row in result.rows:
            if not show_all and row.outcome == "unchanged" and not row.detail:
                continue
            line = f"  row {row.row:<5} {row.outcome:15}"
            if row.report_id:
                line += f" report {row.report_id}"
            if row.changed:
                line += f" [{', '.join(row.changed)}]"
            if row.detail:
                line += f" — {row.detail}"
            # needs_attention rather than the outcome string: a row that applied
            # by overwriting newer work is the loudest line here and used to print
            # unstyled, indistinguishable from an ordinary apply.
            if row.needs_attention:
                self.stdout.write(self.style.WARNING(line))
            else:
                self.stdout.write(line)

        for warning in result.warnings:
            self.stdout.write(self.style.WARNING(f"  {warning}"))

        # Reported before the exit status is decided: a run that applied rows and
        # then tripped an error must still tell the operator which rows are now
        # in their database. Same reason import_security_zip does it this way.
        summary = result.summary()
        if result.error:
            self.stdout.write(self.style.WARNING(summary))
            raise CommandError(result.error)

        style = self.style.WARNING if result.conflicts or result.failed else self.style.SUCCESS
        self.stdout.write(style(summary))
        if result.conflicts:
            # The flag lives here, not in summary(): that string is also rendered
            # verbatim into the dashboard's flash message, where there is no
            # --force — the control there is a checkbox.
            self.stdout.write(
                "  Re-export to pick those rows up, or re-run with --force to let "
                "the sheet overwrite what changed here."
            )
        if result.forced:
            self.stdout.write(
                self.style.WARNING(
                    f"  {result.forced} row(s) overwrote work done after the export. "
                    "The rows above marked 'forced over a newer state' say which."
                )
            )
        if result.dry_run:
            self.stdout.write("Dry run — nothing was written. Re-run without --dry-run to apply.")
