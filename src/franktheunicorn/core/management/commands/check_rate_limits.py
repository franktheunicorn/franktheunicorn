"""Management command to show rate limit status."""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Show GitHub API rate limit status"

    def handle(self, *args: object, **options: object) -> None:
        token = os.environ.get("FRANK_GITHUB_TOKEN", "")
        if not token:
            self.stdout.write(self.style.WARNING("FRANK_GITHUB_TOKEN not set"))
            return
        try:
            import httpx

            resp = httpx.get(
                "https://api.github.com/rate_limit",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            core = data.get("resources", {}).get("core", {})
            self.stdout.write("GitHub API rate limit:")
            self.stdout.write(f"  Remaining: {core.get('remaining', '?')}/{core.get('limit', '?')}")
            self.stdout.write(f"  Reset: {core.get('reset', '?')}")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to check rate limits: {e}"))
