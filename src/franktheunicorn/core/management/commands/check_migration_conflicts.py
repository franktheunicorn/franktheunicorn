"""Management command to detect Django migration conflicts.

Catches migration graph issues early — before ``migrate`` fails at runtime.
Designed to run in CI and pre-push hooks.

Checks performed:
1. **Leaf-node conflicts** — two migrations in the same app that share a
   parent, meaning the migration graph has branched and needs a merge
   migration (``makemigrations --merge``).
2. **Unmade migrations** — model changes that haven't been captured in a
   migration file yet (same as ``makemigrations --check``).
"""

from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState


class Command(BaseCommand):
    help = "Detect migration conflicts and unmade migrations across all Django apps"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--check-unmade",
            action="store_true",
            default=False,
            help="Also check for model changes missing a migration file.",
        )

    def handle(self, *args: object, **options: object) -> None:
        errors: list[str] = []

        # 1. Leaf-node conflicts (branched migration graph).
        loader = MigrationLoader(None, ignore_no_migrations=True)
        conflicts = loader.detect_conflicts()
        if conflicts:
            for app_label, migration_names in sorted(conflicts.items()):
                names = ", ".join(sorted(migration_names))
                errors.append(
                    f"Conflicting migrations in '{app_label}': {names}\n"
                    f"  Run: python manage.py makemigrations --merge"
                )

        # 2. Unmade migrations (model ↔ migration drift).
        if options.get("check_unmade"):
            autodetector = MigrationAutodetector(
                loader.project_state(),
                ProjectState.from_apps(apps),
            )
            changes = autodetector.changes(graph=loader.graph)
            if changes:
                for app_label, app_changes in sorted(changes.items()):
                    count = len(app_changes)
                    s = "s" if count != 1 else ""
                    errors.append(
                        f"Unmade migration{s} detected in '{app_label}' "
                        f"({count} operation{s})\n"
                        f"  Run: python manage.py makemigrations {app_label}"
                    )

        # Report results.
        if errors:
            for error in errors:
                self.stderr.write(self.style.ERROR(error))
            raise CommandError(f"{len(errors)} migration issue(s) found.")

        self.stdout.write(self.style.SUCCESS("No migration conflicts detected."))
