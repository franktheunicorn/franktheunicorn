"""Management command to train the Bayesian rejection predictor (v1.75)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser

from franktheunicorn.core.models import OperatorAction, Project
from franktheunicorn.scoring.rejection_predictor import (
    MIN_ACTIONS_TO_TRAIN,
    RejectionPredictor,
    _model_path_for_project,
)


class Command(BaseCommand):
    help = "Train the Bayesian rejection predictor for one or all projects"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--project",
            help="Project in owner/repo format. Omit to train all projects.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=f"Train even with fewer than {MIN_ACTIONS_TO_TRAIN} operator actions.",
        )

    def handle(self, *args: object, **options: object) -> None:
        project_name: str | None = options.get("project")  # type: ignore[assignment]
        force: bool = options.get("force", False)  # type: ignore[assignment]

        if project_name:
            self._train_project(project_name, force=force)
        else:
            self._train_all(force=force)

    def _train_project(self, project_name: str, *, force: bool = False) -> None:
        parts = project_name.split("/", 1)
        if len(parts) != 2:
            self.stderr.write(self.style.ERROR("--project must be in owner/repo format"))
            return

        owner, repo = parts
        try:
            project = Project.objects.get(owner=owner, repo=repo)
        except Project.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"Project {project_name} not found in database."))
            return

        self._train_single(project, force=force)

    def _train_all(self, *, force: bool = False) -> None:
        projects = Project.objects.filter(enabled=True)
        if not projects.exists():
            self.stdout.write("No enabled projects found.")
            return

        trained = 0
        for project in projects:
            if self._train_single(project, force=force):
                trained += 1

        self.stdout.write(self.style.SUCCESS(f"Trained rejection models for {trained} project(s)."))

    def _train_single(self, project: Project, *, force: bool = False) -> bool:
        action_count = OperatorAction.objects.filter(
            action_type__in=["accept_draft", "reject_draft", "edit_draft"],
            review_draft__pull_request__project=project,
        ).count()

        if action_count < MIN_ACTIONS_TO_TRAIN and not force:
            self.stdout.write(
                f"  {project.full_name}: {action_count} actions "
                f"(need {MIN_ACTIONS_TO_TRAIN}). Skipping."
            )
            return False

        if action_count == 0:
            self.stdout.write(f"  {project.full_name}: No actions. Skipping.")
            return False

        predictor = RejectionPredictor()
        success = predictor.train(project.pk, force=force)

        if not success:
            self.stdout.write(self.style.WARNING(f"  {project.full_name}: Training failed."))
            return False

        model_path = _model_path_for_project(project.owner, project.repo)
        predictor.save(model_path)

        self.stdout.write(
            self.style.SUCCESS(
                f"  {project.full_name}: Trained on {action_count} actions. "
                f"Model saved to {model_path}"
            )
        )
        return True
