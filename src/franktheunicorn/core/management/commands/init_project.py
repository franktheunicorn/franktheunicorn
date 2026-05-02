"""Management command for interactive project initialization."""

from __future__ import annotations

import os
from pathlib import Path

import django.conf
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Initialize franktheunicorn configuration"

    def handle(self, *args: object, **options: object) -> None:
        base = Path(django.conf.settings.BASE_DIR)
        config_dir = Path(os.environ.get("FRANK_CONFIG_DIR", str(base / "config" / "active")))
        config_dir.mkdir(parents=True, exist_ok=True)
        projects_dir = config_dir / "projects"
        projects_dir.mkdir(exist_ok=True)

        operator_path = config_dir / "operator.yaml"
        if operator_path.exists():
            self.stdout.write(f"Operator config already exists at {operator_path}")
        else:
            self.stdout.write("Creating operator config...")
            default_username = ""
            token = os.environ.get("FRANK_GITHUB_TOKEN", "")
            if token:
                from franktheunicorn.backends.github import infer_github_username

                default_username = infer_github_username(token)
                if default_username:
                    self.stdout.write(
                        self.style.SUCCESS(f"  Auto-detected GitHub username: {default_username}")
                    )

            if default_username:
                username = (
                    input(f"GitHub username [{default_username}]: ").strip() or default_username
                )
            else:
                username = input("GitHub username: ").strip()
            style = input("Review style [direct but kind]: ").strip() or "direct but kind"

            operator_path.write_text(
                f"mock_mode: false\n"
                f'github_token: "${{FRANK_GITHUB_TOKEN}}"\n'
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
