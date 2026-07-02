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

        if "/" not in project_name:
            self.stderr.write(self.style.ERROR("--project must be in owner/repo format"))
            return

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
            return

        # Persist scores into the project YAML's collaborator_scores map —
        # that's what the scorer reads (§2.4). Merge, don't overwrite:
        # entries with score null are manual and are never touched.
        saved_to = self._save_scores(project_name, results)
        if saved_to:
            self.stdout.write(
                self.style.SUCCESS(f"\nSaved {len(results)} collaborator scores to {saved_to}.")
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDetected {len(results)} collaborators but no project YAML found "
                    f"for {project_name} — run add_project first, or use --dry-run."
                )
            )

    def _save_scores(
        self,
        project_name: str,
        results: list[tuple[str, int, dict[str, int]]],
    ) -> Path | None:
        """Merge detected scores into the project YAML. Returns the path written."""
        import os

        import yaml
        from django.conf import settings

        owner, repo = project_name.split("/", 1)
        base = Path(settings.BASE_DIR)
        config_dir = Path(os.environ.get("FRANK_CONFIG_DIR", str(base / "config" / "active")))
        yaml_path = config_dir / "projects" / f"{owner}-{repo}.yaml"
        if not yaml_path.is_file():
            return None

        data = yaml.safe_load(yaml_path.read_text()) or {}
        if not isinstance(data, dict):
            return None
        scores = data.get("collaborator_scores") or {}
        if not isinstance(scores, dict):
            scores = {}

        for user, score, _signals in results:
            # score None marks a manual entry (full weight, never overwritten).
            if user in scores and scores[user] is None:
                continue
            scores[user] = score

        data["collaborator_scores"] = scores
        yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))
        return yaml_path


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
