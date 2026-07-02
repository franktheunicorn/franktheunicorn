"""Management command to add a new monitored project."""

from __future__ import annotations

import os
from pathlib import Path

import django.conf
from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = "Add a new project to monitor"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--repo", required=True, help="Repository in owner/repo format")
        parser.add_argument("--governance", default="standard", help="Governance model")
        parser.add_argument("--tone", default="direct", help="Review tone")
        parser.add_argument("--output-dir", help="Output directory for YAML file")

    def handle(self, *args: object, **options: object) -> None:
        repo: str = options["repo"]  # type: ignore[assignment]
        governance: str = options.get("governance", "standard")  # type: ignore[assignment]
        tone: str = options.get("tone", "direct")  # type: ignore[assignment]
        output_dir: str | None = options.get("output_dir")  # type: ignore[assignment]

        parts = repo.split("/", 1)
        if len(parts) != 2:
            self.stderr.write(self.style.ERROR("--repo must be in owner/repo format"))
            return

        owner, repo_name = parts

        if output_dir:
            projects_dir = Path(output_dir)
        else:
            base = Path(django.conf.settings.BASE_DIR)
            config_dir = Path(os.environ.get("FRANK_CONFIG_DIR", str(base / "config" / "active")))
            projects_dir = config_dir / "projects"

        projects_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{owner}-{repo_name}.yaml"
        filepath = projects_dir / filename

        yaml_content = (
            f'owner: "{owner}"\n'
            f'repo: "{repo_name}"\n'
            f'review_context: "general open-source"\n'
            f'governance: "{governance}"\n'
            f'tone: "{tone}"\n'
            f"watched_paths: []\n"
            f"ignore_paths: []\n"
            f"frequent_contributors: []\n"
            f"enabled: true\n"
        )

        filepath.write_text(yaml_content)
        self.stdout.write(self.style.SUCCESS(f"Created {filepath}"))

        # Create the Project row too. The worker's poll would create it
        # eventually, but analyze_repo (which we point the user at next)
        # requires it to exist — without this, add_project and analyze_repo
        # pointed at each other in a dead loop on a fresh install.
        from franktheunicorn.core.models import Project

        _project, created = Project.objects.get_or_create(
            owner=owner,
            repo=repo_name,
            defaults={"review_context": "general open-source", "enabled": True},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Registered project {owner}/{repo_name}"))

        self.stdout.write(
            f"Run 'python manage.py analyze_repo --repo {repo}' "
            f"to bootstrap codebase context from git history."
        )
