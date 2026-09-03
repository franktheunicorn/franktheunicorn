"""Undo the last security-sheet import by restoring its pre-import snapshot."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from franktheunicorn.security.sheet_sync import undo_last_import


class Command(BaseCommand):
    help = (
        "Restore the writable state of every report the last security-sheet "
        "import changed. One level back only — there is no redo, and anything "
        "the sheet did not own (triage runs, dashboard verdicts) is left as it is."
    )

    def handle(self, *args: object, **options: object) -> None:
        result = undo_last_import()
        style = self.style.SUCCESS if result.undone else self.style.NOTICE
        self.stdout.write(style(result.summary()))
