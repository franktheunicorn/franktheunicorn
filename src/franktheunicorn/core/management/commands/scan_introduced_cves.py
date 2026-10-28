"""The scheduled introduced-CVE scan, from the command line.

Walks a project's recent changes (last ``lookback_days`` on daily, diff from
the last final release tag on weekly/monthly) and asks a coding agent whether
any introduce a new CVE, given the project's previously accepted and rejected
CVE reports as context. Files each finding as a SecurityReport with
source="introduced-scan" and queues exploit verification on it.

The same work the worker scheduler queues when a project's cadence is due;
this runs it in the foreground, which is the right tool for the first run on a
project or a one-off outside the cadence. ``--queue`` hands it to the worker
instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.config.loader import get_operator_config

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    help = (
        "Scan a project's recent changes for newly introduced CVEs, file findings "
        "as security reports, and queue exploit verification on each"
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
            from franktheunicorn.security.queue import queue_introduced_scan

            if queue_introduced_scan(project):
                self.stdout.write(f"Queued scan_introduced_cves for {project.full_name}.")
            else:
                self.stdout.write(f"A scan is already queued or running for {project.full_name}.")
            return

        operator_config = get_operator_config()
        from franktheunicorn.security.introduced_scan import scan_project_for_introduced_cves

        result = scan_project_for_introduced_cves(
            project, operator_config, cadence=str(options.get("cadence") or "") or None
        )
        self.stdout.write(result.summary())
        if result.error:
            raise CommandError(result.error)
        for finding in result.findings[: project_config.introduced_scan.max_findings]:
            title = str(finding.get("title", "")).strip()
            severity = str(finding.get("severity", "")).strip() or "unknown"
            self.stdout.write(f"  - {title[:70]} ({severity})")
