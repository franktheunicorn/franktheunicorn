"""Management command to clear persisted LLM backend fallback state.

Use this when upgrading an LLM server, switching endpoint versions, or
otherwise wanting frank to re-probe backend capabilities from scratch.

Usage::

    python manage.py clear_llm_fallbacks
    python manage.py clear_llm_fallbacks --yes   # skip confirmation prompt
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = "Delete all persisted LLM backend fallback rows so capabilities are re-probed."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--yes",
            action="store_true",
            default=False,
            help="Skip the confirmation prompt (useful for scripted invocations).",
        )

    def handle(self, *args: object, **options: object) -> None:
        from franktheunicorn.core.models import LLMBackendFallback

        count = LLMBackendFallback.objects.count()
        if count == 0:
            self.stdout.write("No LLM backend fallback rows to clear.")
            return

        if not options.get("yes"):
            confirm = input(
                f"This will delete {count} LLM backend fallback row(s). Continue? [y/N] "
            )
            if confirm.strip().lower() not in ("y", "yes"):
                self.stdout.write("Aborted.")
                return

        LLMBackendFallback.objects.all().delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} LLM backend fallback row(s)."))
