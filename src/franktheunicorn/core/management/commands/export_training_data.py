"""Management command: export-training-data (v2).

Exports operator action history into JSONL training data for fine-tuning.
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.core.models import Project

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Export training data for fine-tuning a personal model (v2)"

    def add_arguments(self, parser: object) -> None:
        parser.add_argument(  # type: ignore[attr-defined]
            "--project",
            required=True,
            help="Project name (owner/repo or owner-repo)",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--output-dir",
            help="Output directory (default: DATA_DIR/training-data/<project>)",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--force",
            action="store_true",
            help="Export even with fewer than 200 actions",
        )

    def handle(self, **options: object) -> None:
        from franktheunicorn.fine_tuning.data_export import export_training_data

        project_name = str(options["project"])

        # Resolve project.
        if "/" in project_name:
            owner, repo = project_name.split("/", 1)
        else:
            parts = project_name.split("-", 1)
            if len(parts) == 2:
                owner, repo = parts
            else:
                raise CommandError(f"Cannot parse project name: {project_name}")

        try:
            project = Project.objects.get(owner=owner, repo=repo)
        except Project.DoesNotExist:
            raise CommandError(f"Project {owner}/{repo} not found in database") from None

        # Resolve output directory.
        output_dir_str = options.get("output_dir")
        if output_dir_str:
            output_dir = Path(str(output_dir_str))
        else:
            data_dir = Path(getattr(settings, "DATA_DIR", Path.home() / ".review-agent"))
            safe_name = f"{owner}-{repo}"
            output_dir = data_dir / "training-data" / safe_name

        force = bool(options.get("force", False))

        result = export_training_data(
            project.pk,
            output_dir,
            force=force,
            data_dir=Path(getattr(settings, "DATA_DIR", Path.home() / ".review-agent")),
        )

        if result.error:
            raise CommandError(result.error)

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {result.train_count} train + {result.eval_count} eval "
                f"examples to {output_dir}"
            )
        )
        if result.structure_counts:
            self.stdout.write("Structure breakdown:")
            for structure, count in sorted(result.structure_counts.items()):
                self.stdout.write(f"  {structure}: {count}")
