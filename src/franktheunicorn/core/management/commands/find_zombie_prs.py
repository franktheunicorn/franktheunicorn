"""The closed-PR follow-up scan, from the command line.

Walks each JIRA-enabled project's closed-but-unmerged PRs newest-first, keeps
the ones whose JIRA ticket is still open, asks the model whether the ticket
and the fix still make sense and what the fix would need to land today, and
puts them in the dashboard's Zombie Queue. Bounded per run and resumable: run
it again to go further back.

The same work the dashboard's "Hunt for zombies" button queues for the worker;
this runs it in the foreground, which is the right tool for the first pass over
a big backlog — you can watch it, and stop it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.config.loader import get_operator_config
from franktheunicorn.review.zombie import resolve_backend, scan_project, zombie_projects

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    help = (
        "Find closed-but-unmerged PRs whose JIRA is still open, assess them, and put "
        "them in the Zombie Queue"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--project",
            help="Limit to one project, as owner/repo. Default is every JIRA-enabled project.",
        )
        parser.add_argument(
            "--max-prs",
            type=int,
            help="Closed PRs to examine per project this run (default: zombie_queue.max_prs_per_run).",
        )
        parser.add_argument(
            "--max-assessments",
            type=int,
            help=(
                "LLM assessments per project this run (default: "
                "zombie_queue.max_assessments_per_run). 0 finds zombies without asking the model."
            ),
        )
        parser.add_argument(
            "--restart",
            action="store_true",
            help=(
                "Forget where the last run got to and start again from the newest closed PR. "
                "PRs already in the queue are not duplicated."
            ),
        )
        parser.add_argument(
            "--queue",
            action="store_true",
            help="Queue the sweep for the worker instead of running it here.",
        )

    def handle(self, *args: object, **options: object) -> None:
        if options.get("queue"):
            from franktheunicorn.security.queue import queue_zombie_scan

            if queue_zombie_scan():
                self.stdout.write("Queued scan_zombie_prs for the worker.")
            else:
                self.stdout.write("A zombie scan is already queued or running.")
            return

        operator_config = get_operator_config()
        wanted = str(options.get("project") or "").strip().lower()
        projects = [
            project
            for project, _config in zombie_projects()
            if not wanted or project.full_name.lower() == wanted
        ]
        if not projects:
            if wanted:
                msg = (
                    f"{wanted} is not an enabled project with jira.enabled and jira.server set "
                    "in its project YAML"
                )
            else:
                msg = (
                    "no enabled project has jira.enabled and jira.server set, so there is no "
                    "ticket to check a closed PR against"
                )
            raise CommandError(msg)

        max_prs_opt = options.get("max_prs")
        max_prs = max_prs_opt if isinstance(max_prs_opt, int) else None
        max_assessments_opt = options.get("max_assessments")
        max_assessments = max_assessments_opt if isinstance(max_assessments_opt, int) else None
        if (max_assessments is None or max_assessments > 0) and resolve_backend(
            operator_config
        ) is None:
            self.stderr.write(
                "No LLM backend is configured (zombie_queue.llm_backend / llm_backends), so "
                "zombies will be found but not assessed."
            )

        for project in projects:
            self.stdout.write(f"Scanning {project.full_name} ...")
            run = scan_project(
                project,
                operator_config,
                max_prs=max_prs,
                max_assessments=max_assessments,
                restart=bool(options.get("restart")),
            )
            self.stdout.write(run.summary())
            for assessment in run.found:
                assessment.refresh_from_db()
                pr = assessment.pull_request
                verdict = assessment.verdict if assessment.is_assessed else "not yet assessed"
                self.stdout.write(
                    f"  #{pr.number} {pr.title[:70]} — {assessment.jira_ticket_id} "
                    f"({assessment.jira_status or 'open'}): {verdict}"
                )
                if assessment.summary:
                    self.stdout.write(f"      {assessment.summary[:300]}")
                for change in assessment.needed_changes:
                    self.stdout.write(f"      - {change[:200]}")
