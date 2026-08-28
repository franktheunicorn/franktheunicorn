"""Export the security backlog as a CSV for review in a shared spreadsheet."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.core.models import Project, SecurityReport
from franktheunicorn.security.sheet_sync import (
    WRITABLE_COLUMNS,
    export_filename,
    export_reports_csv,
    reports_for_export,
)

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    help = (
        "Export security reports to CSV for review in a shared spreadsheet "
        "(import the edits back with import_security_csv)"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--out",
            help=(
                "Write here instead of stdout. Defaults to a dated name in the "
                "current directory when the flag is given with no value."
            ),
            nargs="?",
            const="",
        )
        parser.add_argument("--project", help="Only reports for this project (owner/repo)")
        parser.add_argument(
            "--status",
            help=(
                "Only reports with this status "
                f"({', '.join(key for key, _ in SecurityReport.STATUS_CHOICES)})"
            ),
        )
        parser.add_argument("--limit", type=int, help="Only the top N by priority")
        parser.add_argument(
            "--full",
            action="store_true",
            help=(
                "Include the raw report text and any proposed patch. Off by "
                "default: they are the two big columns, and a sheet full of them "
                "is a good deal more sensitive to leave sitting in a Drive."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        status = str(options.get("status") or "")
        if status:
            valid = {key for key, _ in SecurityReport.STATUS_CHOICES}
            if status not in valid:
                raise CommandError(f"--status must be one of: {', '.join(sorted(valid))}")

        project = str(options.get("project") or "")
        if project:
            self._check_project(project)

        raw_limit = options.get("limit")
        limit = int(raw_limit) if isinstance(raw_limit, int) else None
        if limit is not None and limit < 1:
            raise CommandError("--limit must be at least 1")

        full = bool(options.get("full"))
        reports = reports_for_export(status=status, project=project, limit=limit)

        destination = options.get("out")
        if destination is None:
            # Straight at self.stdout, which is what makes `--out`-less output
            # redirectable and `call_command(stdout=...)` work. Safe because
            # export_reports_csv sets lineterminator="\n": Django's OutputWrapper
            # only appends its ending when the message doesn't already end with
            # it, so it adds nothing here. Verified — with "\r\n" it would still
            # add nothing, but every row would carry both endings.
            # .iterator(), like the dashboard door. This is the *uncapped* one, so
            # materialising is worse here than there: 2,000 reports carrying 49 KB of
            # raw_text and 49 KB of patch peaked at 199.8 MB resident against 39.5 MB
            # streamed — 5.1x, measured — to write a file that goes out row by row.
            count = export_reports_csv(reports.iterator(chunk_size=200), self.stdout, full=full)
            # Progress on stderr: stdout is the CSV, and a summary line in the
            # middle of it makes the file unimportable by its own importer.
            self.stderr.write(f"{count} report(s) exported.")
            return

        path = Path(str(destination) or export_filename(full=full))
        with path.open("w", encoding="utf-8", newline="") as handle:
            count = export_reports_csv(reports.iterator(chunk_size=200), handle, full=full)

        self.stdout.write(self.style.SUCCESS(f"{count} report(s) → {path}"))
        self.stdout.write(
            "Import it into a spreadsheet, share that with whoever rules on these, "
            f"and edit only: {', '.join(WRITABLE_COLUMNS)}."
        )
        self.stdout.write(
            f"Then download it as CSV and run: manage.py import_security_csv {path.name} --dry-run"
        )
        self.stdout.write(
            self.style.WARNING(
                "Keep the 'check' column. It is what stops a stale sheet "
                "overwriting a ruling you made after exporting it."
            )
        )

    def _check_project(self, spec: str) -> None:
        if "/" not in spec:
            raise CommandError("--project must be in owner/repo format")
        owner, repo = spec.split("/", 1)
        if not Project.objects.filter(owner=owner, repo=repo).exists():
            raise CommandError(f"No project {spec} in the database.")
