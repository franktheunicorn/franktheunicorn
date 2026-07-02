"""Django management command for the Comment Curator CLI."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Interactive TUI for curating voice dataset from historical review comments"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--project",
            required=True,
            help="Project in owner/repo format (e.g. apache/spark)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum number of comments to scrape (default: 100)",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            default="",
            help="Output directory for curated dataset (default: config/active/voice)",
        )

    def handle(self, *args: object, **options: object) -> None:
        project = str(options["project"])
        limit = int(str(options["limit"]))
        output_dir_str = str(options["output_dir"])

        if "/" not in project:
            raise CommandError("--project must be in owner/repo format")

        owner, repo = project.split("/", 1)
        # FRANK_GITHUB_TOKEN is the project's canonical token variable
        # (.env.example, settings, other commands); accept plain GITHUB_TOKEN
        # as a fallback for compatibility.
        token = os.environ.get("FRANK_GITHUB_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise CommandError("FRANK_GITHUB_TOKEN environment variable is required")

        output_dir = Path(output_dir_str) if output_dir_str else None

        from franktheunicorn.curator.scraper import scrape_review_comments

        self.stdout.write(f"Scraping up to {limit} comments from {owner}/{repo}...")
        comments = scrape_review_comments(owner, repo, token, limit=limit)

        if not comments:
            self.stdout.write(self.style.WARNING("No comments found."))
            return

        self.stdout.write(f"Found {len(comments)} comments. Classifying...")

        from franktheunicorn.curator.classifier import classify_comments

        classified = classify_comments(comments)

        self.stdout.write(f"Classified {len(classified)} comments. Launching curator TUI...")

        from franktheunicorn.curator.app import CuratorApp

        app = CuratorApp(
            comments=classified,
            project_name=project,
            output_dir=output_dir,
        )
        app.run()
