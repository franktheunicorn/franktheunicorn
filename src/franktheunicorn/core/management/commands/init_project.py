"""Management command for interactive project initialization."""

from __future__ import annotations

import os
from pathlib import Path

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Initialize franktheunicorn configuration"

    def handle(self, *args: object, **options: object) -> None:
        config_dir = Path(os.environ.get("FRANK_CONFIG_DIR", str(Path.home() / ".review-agent")))
        config_dir.mkdir(parents=True, exist_ok=True)
        projects_dir = config_dir / "projects"
        projects_dir.mkdir(exist_ok=True)

        operator_path = config_dir / "config.yaml"
        if operator_path.exists():
            self.stdout.write(f"Operator config already exists at {operator_path}")
        else:
            self.stdout.write("Creating operator config...")
            username = input("GitHub username: ").strip()
            style = input("Review style [direct but kind]: ").strip() or "direct but kind"

            operator_path.write_text(
                f'github_username: "{username}"\n'
                f'review_style: "{style}"\n'
                f"auto_post: false\n"
                f"poll_interval_seconds: 300\n"
                f'digest_email: ""\n'
                f"digest_enabled: false\n"
                f"llm_backends:\n"
                f"  - provider: stub\n"
            )
            self.stdout.write(self.style.SUCCESS(f"Created {operator_path}"))

        self.stdout.write(self.style.SUCCESS(f"\nConfiguration directory: {config_dir}"))
        self.stdout.write("Next: run 'python manage.py add_project --repo owner/repo'")
