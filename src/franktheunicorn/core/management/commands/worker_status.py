"""Management command to show worker queue status."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from franktheunicorn.core.models import PullRequest, ReviewDraft


class Command(BaseCommand):
    help = "Show worker queue depth and review status"

    def handle(self, *args: object, **options: object) -> None:
        open_prs = PullRequest.objects.filter(state="open").count()
        pending_review = (
            PullRequest.objects.filter(state="open").exclude(review_drafts__isnull=False).count()
        )
        pending_drafts = ReviewDraft.objects.filter(status="pending").count()
        posted_drafts = ReviewDraft.objects.filter(status="posted").count()

        self.stdout.write(f"Open PRs: {open_prs}")
        self.stdout.write(f"PRs awaiting review: {pending_review}")
        self.stdout.write(f"Pending drafts: {pending_drafts}")
        self.stdout.write(f"Posted drafts: {posted_drafts}")
