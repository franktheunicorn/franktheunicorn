"""The variant-analysis scan, from the command line.

For each confirmed past CVE (valid report with a dated introduced_commit),
one coding agent hunts for the same bug pattern elsewhere in the codebase.
Files each candidate as a SecurityReport with source="variant-scan" and
queues verification on it. The same work the worker scheduler queues when a
project's cadence is due; this runs it in the foreground. ``--queue`` hands
it to the worker instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.config.loader import get_operator_config

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    help = (
        "Scan a project's codebase for variants of its confirmed past CVEs, "
        "file findings as security reports, and queue verification on each"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--project",
            required=True,
            help="The project to scan, as owner/repo.",
        )
        parser.add_argument(
            "--cadence",
            choices=["daily", "weekly", "monthly"],
            help="Override the project's configured cadence for this run.",
        )
        parser.add_argument(
            "--queue",
            action="store_true",
            help="Queue the scan for the worker instead of running it here.",
        )

    def handle(self, *args: object, **options: object) -> None:
        from franktheunicorn.config.loader import get_project_config
        from franktheunicorn.core.models import Project

        wanted = str(options.get("project") or "").strip().lower()
        project_config = get_project_config(wanted)
        if project_config is None:
            msg = f"{wanted} is not a configured project"
            raise CommandError(msg)

        project, _created = Project.objects.get_or_create(
            owner=project_config.owner,
            repo=project_config.repo,
            defaults={"review_context": project_config.review_context or ""},
        )

        if options.get("queue"):
            from franktheunicorn.security.queue import queue_variant_scan

            if queue_variant_scan(project):
                self.stdout.write(f"Queued scan_security_variants for {project.full_name}.")
            else:
                self.stdout.write(f"A scan is already queued or running for {project.full_name}.")
            return

        operator_config = get_operator_config()
        from franktheunicorn.security.variant_scan import scan_project_for_variants

        result = scan_project_for_variants(
            project, operator_config, cadence=str(options.get("cadence") or "") or None
        )
        self.stdout.write(result.summary())
        if result.error:
            raise CommandError(result.error)
