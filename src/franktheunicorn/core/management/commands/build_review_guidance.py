"""Build review guidance from a project's CVE-introducing commits.

Walks the project's valid reports that have a dated introducing commit,
shows each commit's diff, asks an LLM what a code reviewer should have
looked for to catch that class of issue at review time, then distills the
per-commit notes into a single guidance addendum. Stores it as a
``SecurityReviewGuidance`` row that the introduced-CVE scan injects into
its prompt — so the scan that looks for new CVEs is guided by what past
CVE-introducing commits looked like.

``--dry-run`` prints the guidance without storing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.config.loader import get_operator_config

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    help = (
        "Build review guidance from a project's CVE-introducing commits and "
        "store it for the introduced-CVE scan to inject"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--project",
            required=True,
            help="The project to build guidance for, as owner/repo.",
        )
        parser.add_argument(
            "--max-commits",
            type=int,
            default=50,
            help="Cap on CVE-introducing commits to review (newest valid first).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the guidance without storing it.",
        )

    def handle(self, *args: object, **options: object) -> None:
        from franktheunicorn.config.loader import get_project_config
        from franktheunicorn.core.models import Project
        from franktheunicorn.security.review_guidance import build_review_guidance

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

        operator_config = get_operator_config()
        max_commits = options.get("max_commits")
        result = build_review_guidance(
            project,
            operator_config,
            max_commits=int(max_commits) if isinstance(max_commits, int) else 50,
            dry_run=bool(options.get("dry_run")),
        )
        self.stdout.write(result.summary())
        if result.error:
            raise CommandError(result.error)
        if options.get("dry_run") and result.guidance_text:
            self.stdout.write("\n--- Guidance (dry run) ---\n")
            self.stdout.write(result.guidance_text)
            self.stdout.write("\n--- end ---\n")
