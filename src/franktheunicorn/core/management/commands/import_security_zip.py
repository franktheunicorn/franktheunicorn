"""Management command to bulk-import security reports from a zip archive."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.core.models import Project
from franktheunicorn.security.zip_import import import_reports_from_zip

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    help = "Import security reports from a zip archive (one report per file)"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("archive", help="Path to the .zip file of reports")
        parser.add_argument(
            "--project",
            help="Attach every imported report to this project (owner/repo)",
        )
        parser.add_argument(
            "--triage",
            action="store_true",
            help=(
                "Queue triage for every imported report. Off by default: bulk import "
                "means one NVD lookup and two LLM calls per report, so this is opt-in "
                "even when security_triage.auto_triage is on."
            ),
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help=(
                "Queue the deep verifier for every imported report — an agent reads "
                "the code and says whether the vulnerability is actually there, once "
                "per active branch. Off by default and more expensive than --triage: "
                "a full agent run per report per branch. Needs "
                "security_triage.verifier.enabled."
            ),
        )
        parser.add_argument(
            "--no-filter",
            action="store_true",
            help=(
                "Import every text entry, not just ones that read like security "
                "reports. Off by default: a handover archive full of source files "
                "(or a stray private key) otherwise becomes a pile of reports."
            ),
        )
        parser.add_argument(
            "--verbose-entries",
            action="store_true",
            help="List the outcome of every entry, not just the ones that missed",
        )

    def handle(self, *args: object, **options: object) -> None:
        archive = Path(str(options["archive"]))
        if not archive.is_file():
            raise CommandError(f"No such file: {archive}")

        project = self._resolve_project(options.get("project"))  # type: ignore[arg-type]

        result = import_reports_from_zip(
            archive,
            project=project,
            auto_triage=bool(options.get("triage")),
            auto_verify=bool(options.get("verify")),
            require_security_content=not options.get("no_filter"),
        )

        # Report before deciding the exit status. Raising on result.error first
        # meant a run that committed reports and *then* tripped a cap exited 1
        # with no mention of the rows now sitting in the operator's database —
        # the exact silent-drop this command's own principle argues against.
        show_all = bool(options.get("verbose_entries"))
        for entry in result.entries:
            if not show_all and entry.outcome in ("imported", "duplicate"):
                continue
            line = f"  {entry.outcome:12} {entry.name}"
            if entry.detail:
                line += f" — {entry.detail}"
            if entry.outcome in ("error", "too-large"):
                self.stdout.write(self.style.WARNING(line))
            else:
                self.stdout.write(line)

        summary = result.summary()
        if result.error:
            self.stdout.write(self.style.WARNING(summary))
            raise CommandError(result.error)

        self.stdout.write(self.style.SUCCESS(summary))
        # Named separately from triage's: an operator who passed --verify and got
        # nothing needs the reason, and "verifier.enabled is false" is a setting
        # they may never have seen.
        if options.get("verify") and result.verify_skipped_reason:
            self.stdout.write(
                self.style.WARNING(f"  Not verified: {result.verify_skipped_reason}.")
            )
        if result.imported and not result.queued_triage:
            if result.triage_skipped_reason:
                self.stdout.write(
                    self.style.WARNING(f"  Imported untriaged: {result.triage_skipped_reason}.")
                )
            elif options.get("triage"):
                self.stdout.write("  Imported untriaged: nothing queued — check the worker log.")
            else:
                self.stdout.write(
                    "  Imported untriaged. Re-run with --triage, or triage from the dashboard."
                )

    def _resolve_project(self, spec: str | None) -> Project | None:
        if not spec:
            return None
        if "/" not in spec:
            raise CommandError("--project must be in owner/repo format")
        owner, repo = spec.split("/", 1)
        project = Project.objects.filter(owner=owner, repo=repo).first()
        if project is None:
            raise CommandError(f"No project {spec} in the database; add it first.")
        return project
