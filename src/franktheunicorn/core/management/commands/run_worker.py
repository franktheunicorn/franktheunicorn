"""Management command to start the background worker."""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start the franktheunicorn background worker"  # noqa: A003

    def handle(self, *args: object, **options: object) -> None:
        from franktheunicorn.worker.runner import run_worker

        self.stdout.write(self.style.SUCCESS("Starting worker..."))
        run_worker()
