"""Management command to add a new monitored project."""

from __future__ import annotations

import os
from pathlib import Path

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
            config_dir = Path(
                os.environ.get("FRANK_CONFIG_DIR", str(Path.home() / ".review-agent"))
            )
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
