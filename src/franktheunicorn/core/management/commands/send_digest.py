"""Management command to send the daily email digest."""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Build and send the daily email digest"

    def add_arguments(self, parser: object) -> None:
        pass

    def handle(self, *args: object, **options: object) -> None:
        from franktheunicorn.digest.service import (
            build_daily_digest,
            render_digest_text,
            send_digest,
        )

        digest = build_daily_digest()
        self.stdout.write(render_digest_text(digest))
        self.stdout.write("\n---\n")

        sent = send_digest(digest)
        if sent:
            self.stdout.write(self.style.SUCCESS("Digest email sent."))
        else:
            self.stdout.write(self.style.WARNING("Digest email not sent (no email configured)."))
