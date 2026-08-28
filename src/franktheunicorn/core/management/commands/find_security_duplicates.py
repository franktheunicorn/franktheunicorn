"""Link duplicate security reports across the whole backlog.

Triage links a report as it goes through, which handles everything arriving from
now on. This is for what's already there — a few hundred reports imported before
the feature existed, which is precisely the pile where duplicates matter most and
where nothing has looked for them.

Read-only unless ``--apply`` is passed. The default prints what it would link and
changes nothing, because the whole feature is a heuristic and the first thing
anyone sensible does with a heuristic is look at its output before letting it write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand

from franktheunicorn.config.loader import get_operator_config
from franktheunicorn.core.models import Project, SecurityReport
from franktheunicorn.security.duplicates import (
    build_signature,
    detect_across_backlog,
    score_pair,
)

if TYPE_CHECKING:
    from django.core.management.base import CommandParser

    from franktheunicorn.config.models import SecurityDuplicateConfig


class Command(BaseCommand):
    help = "Find and link security reports that look like duplicates of each other"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write the links. Without this, only reports what it would do.",
        )
        parser.add_argument(
            "--project",
            help="Limit to one project, as owner/repo. Default is every project.",
        )
        parser.add_argument(
            "--threshold",
            type=float,
            help=(
                "Override security_triage.duplicates.threshold for this run — useful "
                "for seeing what a looser setting would catch before committing to it."
            ),
        )
        parser.add_argument(
            "--relink",
            action="store_true",
            help=(
                "Reconsider reports that already have a detected link. Off by "
                "default so a second run doesn't churn links you've already read. "
                "Never touches a link with no confidence score — that's one you set."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        config = get_operator_config().security_triage.duplicates
        if options.get("threshold") is not None:
            # model_validate, not model_copy: model_copy does NOT run validators in
            # Pydantic v2, so `--threshold -1` was accepted — and since find_duplicate
            # only skips on `score < threshold`, a negative one matches every pair and
            # `--apply` would link every report in the project.
            try:
                config = config.model_validate(
                    {**config.model_dump(), "threshold": float(options["threshold"])}  # type: ignore[arg-type]
                )
            except ValueError as exc:
                self.stderr.write(self.style.ERROR(f"Bad --threshold: {exc}"))
                return

        if not config.enabled:
            # Unconditional, and that is the fix rather than a tidy-up: this was
            # suppressed on --apply, i.e. on the one run that writes to the database.
            # Reported rather than obeyed, because an operator running this by hand has
            # asked for it and the config flag describes the automatic path.
            self.stdout.write(
                self.style.WARNING(
                    "security_triage.duplicates.enabled is false, which turns this off "
                    "during triage. Running anyway because you asked directly."
                )
            )

        reports = SecurityReport.objects.all()
        project_name = options.get("project")
        if project_name:
            # full_name is a property, not a column, so it can't be filtered on.
            owner, _, repo = str(project_name).partition("/")
            project = Project.objects.filter(owner=owner, repo=repo).first()
            if project is None:
                self.stderr.write(
                    self.style.ERROR(
                        f"No project {project_name!r}. Expected owner/repo; known: "
                        + (
                            ", ".join(p.full_name for p in Project.objects.all()[:20])
                            or "(none configured)"
                        )
                    )
                )
                return
            reports = reports.filter(project=project)

        if not options.get("relink"):
            # Skip anything already linked, so a second run doesn't churn links you
            # have read. This filtered on `duplicate_confidence__isnull=True`, which
            # is inverted: that field is NULL for unlinked reports *and* for
            # hand-linked ones, and non-NULL only for detected links — so the default
            # run fed every hand-set link back through detection and excluded exactly
            # the detected ones it meant to skip. `link_duplicate` now refuses to
            # overwrite a hand-set link regardless, but the filter should still say
            # what it means.
            reports = reports.filter(duplicate_of__isnull=True)

        candidates = list(reports.select_related("project", "duplicate_of"))
        if not candidates:
            self.stdout.write("No reports to consider.")
            return

        self.stdout.write(
            f"Comparing {len(candidates)} report(s) at threshold {config.threshold:.2f}"
            + (" — DRY RUN, nothing will be written." if not options.get("apply") else "")
        )

        if options.get("apply"):
            linked = detect_across_backlog(candidates, config)
            self.stdout.write(
                self.style.SUCCESS(f"Linked {linked} report(s) as probable duplicates.")
            )
            self.stdout.write(
                "Nothing was marked as status=duplicate — that verdict is yours. "
                "The links are on the report pages."
            )
            return

        self._report_dry_run(candidates, config)

    def _report_dry_run(
        self, candidates: list[SecurityReport], config: SecurityDuplicateConfig
    ) -> None:
        """Print the links that would be made.

        Duplicates the pairing loop from ``detect_across_backlog`` rather than
        calling it, because that one writes. Same ordering — oldest first, so links
        point backwards in time at the report carrying the accumulated triage.
        """
        ordered = sorted(candidates, key=lambda r: (r.created_at, r.pk))
        signatures = [(report, build_signature(report)) for report in ordered]
        found = 0
        for index, (report, signature) in enumerate(signatures):
            best = None
            for earlier, earlier_sig in signatures[:index]:
                if earlier.project_id != report.project_id:
                    continue
                match = score_pair(signature, earlier_sig, config)
                if match.score < config.threshold:
                    continue
                if best is None or match.score > best[0].score:
                    best = (match, earlier)
            if best is None:
                continue
            found += 1
            match, earlier = best
            self.stdout.write(f"  #{report.pk} -> #{earlier.pk}  {match.score:.2f}  {match.reason}")
            self.stdout.write(f"      this : {(report.title or report.raw_text)[:90]!r}")
            self.stdout.write(f"      that : {(earlier.title or earlier.raw_text)[:90]!r}")

        if found:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{found} probable duplicate(s). Re-run with --apply to write the "
                    "links, or --threshold to try a different cutoff first."
                )
            )
        else:
            self.stdout.write(
                f"No duplicates found above {config.threshold:.2f}. "
                "Try --threshold with a lower value to see what's just below the line."
            )
