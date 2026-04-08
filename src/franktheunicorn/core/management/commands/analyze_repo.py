"""Management command to run repo health analysis on a project."""

from __future__ import annotations

from pathlib import Path

import django.conf
from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone


class Command(BaseCommand):
    help = "Analyze repository health (churn, contributors, bugs, momentum, emergencies)"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--repo", required=True, help="Repository in owner/repo format")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-analyze even if a recent snapshot exists",
        )

    def handle(self, *args: object, **options: object) -> None:
        repo: str = options["repo"]  # type: ignore[assignment]
        force: bool = options.get("force", False)  # type: ignore[assignment]

        parts = repo.split("/", 1)
        if len(parts) != 2:
            self.stderr.write(self.style.ERROR("--repo must be in owner/repo format"))
            return

        owner, repo_name = parts

        from franktheunicorn.core.models import Project
        from franktheunicorn.worker.repo_health import (
            analyze_repo_health,
            snapshot_to_dict,
        )
        from franktheunicorn.worker.repo_manager import ensure_repo

        project = Project.objects.filter(owner=owner, repo=repo_name).first()
        if project is None:
            self.stderr.write(
                self.style.ERROR(
                    f"Project {owner}/{repo_name} not found. "
                    f"Run 'python manage.py add_project --repo {repo}' first."
                )
            )
            return

        if project.repo_health_analyzed_at and not force:
            self.stdout.write(
                f"Existing snapshot from {project.repo_health_analyzed_at}. "
                f"Use --force to re-analyze."
            )
            return

        repos_dir = Path(django.conf.settings.FRANK_REPOS_DIR)
        repo_path = ensure_repo(repos_dir, owner, repo_name)
        if repo_path is None:
            self.stderr.write(self.style.ERROR(f"Failed to clone/fetch {owner}/{repo_name}"))
            return

        self.stdout.write(f"Analyzing {owner}/{repo_name} ...")
        snapshot = analyze_repo_health(repo_path)

        project.repo_health_snapshot = snapshot_to_dict(snapshot)
        project.repo_health_analyzed_at = timezone.now()
        project.save(update_fields=["repo_health_snapshot", "repo_health_analyzed_at"])

        # Print summary
        self.stdout.write(self.style.SUCCESS(f"\nRepo health analysis for {owner}/{repo_name}:"))

        if snapshot.high_churn_files:
            self.stdout.write(f"\n  High-churn files (top {len(snapshot.high_churn_files)}):")
            for entry in snapshot.high_churn_files[:10]:
                self.stdout.write(f"    {entry.commit_count:4d} commits  {entry.file_path}")

        if snapshot.contributors:
            total = sum(c.commit_count for c in snapshot.contributors)
            top = snapshot.contributors[0]
            pct = round(top.commit_count / total * 100) if total else 0
            self.stdout.write(
                f"\n  Contributors: {len(snapshot.contributors)} total, "
                f"top: {top.author} ({pct}% of {total} commits)"
            )

        if snapshot.bug_hotspots:
            self.stdout.write(f"\n  Bug hotspots (top {len(snapshot.bug_hotspots)}):")
            for hotspot in snapshot.bug_hotspots[:10]:
                self.stdout.write(
                    f"    {hotspot.bug_commit_count:4d} bug commits  {hotspot.file_path}"
                )

        if snapshot.monthly_commits:
            recent = snapshot.monthly_commits[-3:]
            avg = sum(m.count for m in recent) // len(recent) if recent else 0
            self.stdout.write(f"\n  Recent momentum: ~{avg} commits/month (3-month avg)")

        if snapshot.emergency_commits:
            self.stdout.write(
                f"\n  Emergency commits (past year): {len(snapshot.emergency_commits)}"
            )
            for line in snapshot.emergency_commits[:5]:
                self.stdout.write(f"    {line}")

        self.stdout.write(self.style.SUCCESS("\nSnapshot saved to database."))
