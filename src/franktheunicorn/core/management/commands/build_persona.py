"""Management command: build-persona.

Scrapes GitHub review comment history for a user, analyses their reviewing
patterns, and generates a personality markdown file that the agent can use
as a named reviewer persona.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.curator.scraper import RawComment

logger = logging.getLogger(__name__)

# Default personalities output directory (operator override location).
_DEFAULT_PERSONALITIES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent.parent.parent
    / "config"
    / "active"
    / "personalities"
)


class Command(BaseCommand):
    help = (
        "Build a reviewer persona from GitHub review history. "
        "Writes a personality markdown file to config/active/personalities/<user>.md."
    )

    def add_arguments(self, parser: object) -> None:
        parser.add_argument(  # type: ignore[attr-defined]
            "--user",
            required=True,
            help="GitHub username to build the persona for",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--project",
            default="",
            help="Scope scraping to this repo (owner/repo). Required unless --repos is given.",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--repos",
            nargs="*",
            default=[],
            help=(
                "Additional repos to scrape in owner/repo format. "
                "Used with scrape_user_comments() for cross-repo history."
            ),
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--limit",
            type=int,
            default=200,
            help="Maximum number of comments to scrape (default: 200)",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--output",
            default="",
            help="Output path for the persona file (default: config/active/personalities/<user>.md)",
        )
        parser.add_argument(  # type: ignore[attr-defined]
            "--refresh",
            action="store_true",
            help="Overwrite an existing persona file",
        )

    def handle(self, **options: object) -> None:
        from franktheunicorn.curator.classifier import classify_comments
        from franktheunicorn.personalities import refresh_personality
        from franktheunicorn.personalities.builder import build_persona_from_comments

        user = str(options["user"])
        project = str(options.get("project", "") or "")
        repos_opt = options.get("repos")
        extra_repos_raw: list[str] = (
            [str(r) for r in repos_opt]  # type: ignore[attr-defined]
            if repos_opt
            else []
        )
        limit = int(str(options.get("limit", 200)))
        output_str = str(options.get("output", "") or "")
        do_refresh = bool(options.get("refresh", False))

        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise CommandError(
                "GITHUB_TOKEN environment variable is required to scrape GitHub comments."
            )

        # Resolve output path.
        output_path = Path(output_str) if output_str else _DEFAULT_PERSONALITIES_DIR / f"{user}.md"

        if output_path.exists() and not do_refresh:
            raise CommandError(
                f"Persona file already exists: {output_path}\n"
                "Use --refresh to overwrite, or --output to specify a different path."
            )

        # Scrape comments.
        raw_comments: list[RawComment] = self._scrape_comments(
            user=user,
            project=project,
            extra_repos=extra_repos_raw,
            token=token,
            limit=limit,
        )

        if not raw_comments:
            raise CommandError(
                f"No review comments found for user '{user}'. "
                "Check that GITHUB_TOKEN has repo access and --project or --repos is correct."
            )

        self.stdout.write(f"Scraped {len(raw_comments)} comments. Classifying...")

        classified = classify_comments(raw_comments)

        # Optionally use an LLM backend for richer synthesis.
        backend_config: LLMBackendConfig | None = self._maybe_load_backend()

        self.stdout.write("Building persona...")
        persona_md = build_persona_from_comments(
            username=user,
            classified_comments=classified,
            backend_config=backend_config,
        )

        # Write output file.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(persona_md, encoding="utf-8")

        # Invalidate personality cache so the new file takes effect immediately.
        refresh_personality(user)

        # Summary.
        cat_counts: dict[str, int] = {}
        for cc in classified:
            cat_counts[cc.category] = cat_counts.get(cc.category, 0) + 1

        self.stdout.write(self.style.SUCCESS(f"Persona written to {output_path}"))
        self.stdout.write("Category breakdown:")
        for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
            self.stdout.write(f"  {cat}: {count}")

        self.stdout.write("")
        self.stdout.write(
            f"To use this persona, set  personality: {user}  in your project YAML config."
        )

    def _scrape_comments(
        self,
        *,
        user: str,
        project: str,
        extra_repos: list[str],
        token: str,
        limit: int,
    ) -> list[RawComment]:
        """Scrape comments by user from the given repo(s)."""
        from franktheunicorn.curator.scraper import (
            scrape_review_comments,
            scrape_user_comments,
        )

        # Collect all repos to scrape.
        repos: list[tuple[str, str]] = []
        if project:
            if "/" not in project:
                raise CommandError(f"--project must be in owner/repo format, got: {project!r}")
            owner, repo = project.split("/", 1)
            repos.append((owner, repo))

        for r in extra_repos:
            if "/" not in r:
                raise CommandError(f"Each --repos entry must be owner/repo, got: {r!r}")
            o, rp = r.split("/", 1)
            repos.append((o, rp))

        if not repos:
            raise CommandError(
                "At least one of --project or --repos must be specified to scrape comments."
            )

        if len(repos) == 1:
            owner, repo = repos[0]
            self.stdout.write(f"Scraping up to {limit} comments by {user} from {owner}/{repo}...")
            return scrape_review_comments(owner, repo, token, limit=limit, author=user)

        self.stdout.write(f"Scraping up to {limit} comments by {user} across {len(repos)} repos...")
        return scrape_user_comments(user, repos, token, limit=limit)

    def _maybe_load_backend(self) -> LLMBackendConfig | None:
        """Return the first configured LLM backend config, or None."""
        try:
            from franktheunicorn.config.loader import load_operator_config

            operator_config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
            if operator_config.llm_backends:
                backend = operator_config.llm_backends[0]
                if backend.provider not in ("stub", ""):
                    return backend
        except Exception:
            logger.debug("Could not load operator config for LLM synthesis", exc_info=True)
        return None
