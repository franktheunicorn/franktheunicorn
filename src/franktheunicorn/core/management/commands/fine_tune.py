"""Management command: fine-tune (v2).

Orchestrates the full fine-tuning pipeline: export, config gen, training, eval.
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.core.models import Project

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Fine-tune a personal model on operator feedback (v2)"

    def add_arguments(self, parser: object) -> None:
        parser.add_argument(  # type: ignore[attr-defined]
            "--project",
            required=True,
            help="Project name (owner/repo or owner-repo)",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--base-model",
            default="",
            help="Override base model (default from operator config)",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--eval-only",
            action="store_true",
            help="Only run evaluation, skip training",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--force",
            action="store_true",
            help="Force training even with insufficient data",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--docker",
            action="store_true",
            help="Run training via Docker instead of locally",
        )

    def handle(self, **options: object) -> None:
        from franktheunicorn.config.loader import load_operator_config
        from franktheunicorn.fine_tuning.axolotl_config import generate_axolotl_config
        from franktheunicorn.fine_tuning.data_export import export_training_data
        from franktheunicorn.fine_tuning.trainer import run_fine_tune

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

        force = bool(options.get("force", False))
        eval_only = bool(options.get("eval_only", False))
        use_docker = bool(options.get("docker", False))

        data_dir = Path(getattr(settings, "DATA_DIR", Path.home() / ".review-agent"))
        safe_name = f"{owner}-{repo}"
        dataset_dir = data_dir / "training-data" / safe_name
        models_dir = data_dir / "models"

        # Step 1: Export training data.
        if not eval_only:
            self.stdout.write("Exporting training data...")
            export_result = export_training_data(
                project.pk, dataset_dir, force=force, data_dir=data_dir
            )
            if export_result.error:
                raise CommandError(f"Export failed: {export_result.error}")
            self.stdout.write(
                f"  {export_result.train_count} train + {export_result.eval_count} eval examples"
            )

        # Step 2: Generate Axolotl config.
        operator_config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
        base_model = (
            str(options.get("base_model", "")) or operator_config.fine_tuning.default_base_model
        )

        self.stdout.write(f"Generating Axolotl config (base: {base_model})...")
        config_result = generate_axolotl_config(
            project.full_name,
            dataset_dir,
            base_model=base_model,
            models_dir=models_dir,
            quantization=operator_config.fine_tuning.quantization,
        )
        if config_result.error:
            raise CommandError(f"Config generation failed: {config_result.error}")
        self.stdout.write(f"  Config: {config_result.config_path}")
        self.stdout.write(f"  Output: {config_result.output_dir} (v{config_result.version})")

        # Step 3: Run training.
        self.stdout.write("Running fine-tuning..." if not eval_only else "Running eval-only...")
        training_result = run_fine_tune(
            project.full_name,
            dataset_dir,
            config_result.config_path,
            config_result.output_dir,
            config_result.version,
            use_docker=use_docker,
            force=force,
            eval_only=eval_only,
        )

        if training_result.error:
            raise CommandError(f"Training failed: {training_result.error}")

        if training_result.success:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Fine-tuning complete! Model saved to {training_result.model_dir}"
                )
            )
        else:
            self.stdout.write("Eval-only mode — no training performed.")

        if training_result.model_dir:
            self.stdout.write(f"  Version: v{training_result.version}")
