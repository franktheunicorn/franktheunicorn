"""Management command to auto-detect collaborators from git history + reviews."""

from __future__ import annotations

import logging
import subprocess
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandParser

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Detect collaborators for a project from git log and review history"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--project", required=True, help="Project full_name (owner/repo)")
        parser.add_argument("--repo-path", help="Path to local git clone")
        parser.add_argument("--dry-run", action="store_true", help="Show results without saving")
        parser.add_argument("--months", type=int, default=6, help="Months of history to analyze")

    def handle(self, *args: object, **options: object) -> None:
        project_name: str = options["project"]  # type: ignore[assignment]
        repo_path: str | None = options.get("repo_path")  # type: ignore[assignment]
        dry_run: bool = options.get("dry_run", False)  # type: ignore[assignment]
        months: int = options.get("months", 6)  # type: ignore[assignment]

        self.stdout.write(f"Detecting collaborators for {project_name} (last {months} months)...")

        # Get operator username from config.
        from django.conf import settings

        from franktheunicorn.config.loader import load_operator_config

        operator_config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
        operator = operator_config.github_username

        if not operator:
            self.stderr.write(self.style.ERROR("operator.github_username not configured"))
            return

        # Analyze git log for co-file committers.
        co_committers: Counter[str] = Counter()
        co_authors: Counter[str] = Counter()

        if repo_path:
            path = Path(repo_path)
            if path.is_dir():
                co_committers = _analyze_git_log(path, operator, months)
                co_authors = _analyze_co_authors(path, operator, months)
            else:
                self.stderr.write(f"Repo path {repo_path} not found")

        # Analyze review history from DB.
        review_freq: Counter[str] = Counter()
        try:
            from franktheunicorn.core.models import PullRequest

            prs = PullRequest.objects.filter(
                project__owner=project_name.split("/")[0],
                project__repo=project_name.split("/")[1],
            ).exclude(author=operator)

            for pr in prs:
                reviewers: list[str] = pr.requested_reviewers or []
                for reviewer in reviewers:
                    if reviewer.lower() == operator.lower():
                        review_freq[pr.author] += 1
        except Exception:
            logger.debug("Could not analyze review history from DB", exc_info=True)

        # Score collaborators.
        all_users = set(co_committers.keys()) | set(co_authors.keys()) | set(review_freq.keys())
        all_users.discard(operator.lower())

        results: list[tuple[str, int, dict[str, int]]] = []
        for user in sorted(all_users):
            signals = {}
            score = 0
            if user in co_committers:
                signals["co_file"] = min(co_committers[user], 25)
                score += signals["co_file"]
            if user in co_authors:
                signals["co_author"] = min(co_authors[user] * 10, 20)
                score += signals["co_author"]
            if user in review_freq:
                signals["review_freq"] = min(review_freq[user] * 5, 10)
                score += signals["review_freq"]
            results.append((user, min(score, 100), signals))

        results.sort(key=lambda x: x[1], reverse=True)

        # Display results.
        self.stdout.write(f"\nFound {len(results)} potential collaborators:\n")
        for user, score, signals in results[:20]:
            sig_str = ", ".join(f"{k}: {v}" for k, v in signals.items())
            self.stdout.write(f"  {user:30s} score: {score:3d}  ({sig_str})")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n--dry-run: no changes saved"))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"\nDetected {len(results)} collaborators for {project_name}.")
            )


def _analyze_git_log(repo_path: Path, operator: str, months: int) -> Counter[str]:
    """Analyze git log for co-file committers."""
    co_committers: Counter[str] = Counter()
    try:
        result = subprocess.run(
            ["git", "log", f"--since={months} months ago", "--format=%aN", "--no-merges"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=60,
        )
        if result.returncode == 0:
            for author in result.stdout.strip().split("\n"):
                author = author.strip().lower()
                if author and author != operator.lower():
                    co_committers[author] += 1
    except Exception:
        logger.debug("git log analysis failed", exc_info=True)
    return co_committers


def _analyze_co_authors(repo_path: Path, operator: str, months: int) -> Counter[str]:
    """Analyze git log for Co-authored-by trailers."""
    co_authors: Counter[str] = Counter()
    try:
        result = subprocess.run(
            ["git", "log", f"--since={months} months ago", "--format=%b"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=60,
        )
        if result.returncode == 0:
            import re

            for line in result.stdout.split("\n"):
                match = re.match(r"Co-authored-by:\s+(.+?)\s+<", line, re.IGNORECASE)
                if match:
                    name = match.group(1).strip().lower()
                    if name != operator.lower():
                        co_authors[name] += 1
    except Exception:
        logger.debug("Co-author analysis failed", exc_info=True)
    return co_authors
