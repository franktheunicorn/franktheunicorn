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
from franktheunicorn.security.duplicates import detect_across_backlog

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
        parser.add_argument(
            "--llm",
            action="store_true",
            help=(
                "Use the LLM title-grouping pass (what triage and the dashboard "
                "re-check use) instead of the local heuristic. One model call per "
                "project per few hundred reports; also clears stale auto-links the "
                "model saw both halves of and didn't group. Needs a configured "
                "backend."
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

        if options.get("llm"):
            self._run_llm(candidates, config, apply=bool(options.get("apply")))
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

    def _run_llm(
        self, candidates: list[SecurityReport], config: SecurityDuplicateConfig, *, apply: bool
    ) -> None:
        """The LLM title-grouping pass: what triage and the dashboard re-check use.

        The dry run prints the groups the model called out; ``--apply`` writes
        them through :func:`redetect_across_backlog`, which also clears stale
        auto-links the model saw both halves of and declined to group.
        """
        from franktheunicorn.security.duplicates import (
            bucket_by_project,
            llm_duplicate_sweep,
            redetect_across_backlog,
        )
        from franktheunicorn.security.triage import resolve_triage_backend

        backend = resolve_triage_backend(get_operator_config())
        if backend is None:
            self.stderr.write(
                self.style.ERROR("No LLM backend configured. Add one to operator.yaml.")
            )
            return

        if apply:
            result = redetect_across_backlog(candidates, config, backend)
            if result is None:
                self.stderr.write(
                    self.style.ERROR("Every LLM call failed — nothing was written. See the log.")
                )
                return
            linked, cleared = result
            self.stdout.write(
                self.style.SUCCESS(
                    f"Linked {linked} report(s) as probable duplicates"
                    + (f", cleared {cleared} stale link(s)." if cleared else ".")
                )
            )
            self.stdout.write(
                "Nothing was marked as status=duplicate — that verdict is yours. "
                "The links are on the report pages."
            )
            return

        # Dry run: the same bucketing --apply sweeps, printed, nothing written.
        buckets = bucket_by_project(sorted(candidates, key=lambda r: (r.created_at, r.pk)))
        by_id = {report.pk: report for report in candidates}
        found = 0
        for project_id, bucket in buckets.items():
            if len(bucket) < 2:
                continue
            sweep = llm_duplicate_sweep(bucket, backend, project_id=project_id)
            if sweep is None:
                self.stderr.write(
                    self.style.ERROR(f"LLM grouping failed for project {project_id}; skipping it.")
                )
                continue
            for group in sweep.groups:
                found += len(group.ids) - 1
                self.stdout.write(
                    f"  group ({group.confidence}): {', '.join(f'#{i}' for i in group.ids)}"
                    f"  — {group.reason}"
                )
                for report_id in group.ids:
                    report = by_id[report_id]
                    self.stdout.write(f"      #{report_id}: {(report.title or '')[:90]!r}")
        if found:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{found} probable duplicate(s) in the model's groups. "
                    "Re-run with --llm --apply to write the links."
                )
            )
        else:
            self.stdout.write("The model found no duplicates in these titles.")

    def _report_dry_run(
        self, candidates: list[SecurityReport], config: SecurityDuplicateConfig
    ) -> None:
        """Print the links that would be made.

        Shares ``plan_duplicates`` and ``would_link`` with ``--apply`` rather than
        keeping its own copy of the pairing loop. The copies diverged the moment
        ``link_duplicate`` grew a guard: this printed ``#2 -> #1`` for a report the
        operator had linked by hand, and ``--apply`` then refused it and said
        "Linked 0". A preview that misreports the write is worse than no preview.
        """
        from franktheunicorn.core.models import SecurityReport as Report
        from franktheunicorn.security.duplicates import (
            plan_duplicates,
            resolve_canonical,
            would_link,
        )

        planned = plan_duplicates(candidates, config)
        found = 0
        skipped = 0
        for report, match in planned:
            if not would_link(report, match):
                skipped += 1
                continue
            found += 1
            target = Report.objects.filter(pk=match.report_id).first()
            # The canonical end of the chain, which is what --apply writes. Printing
            # the raw match would show a link to a row that is itself a pointer.
            if target is not None:
                target = resolve_canonical(target)
            target_pk = target.pk if target else match.report_id
            self.stdout.write(f"  #{report.pk} -> #{target_pk}  {match.score:.2f}  {match.reason}")
            self.stdout.write(f"      this : {(report.title or report.raw_text)[:90]!r}")
            if target is not None:
                self.stdout.write(f"      that : {(target.title or target.raw_text)[:90]!r}")

        if skipped:
            # Counted rather than hidden: these scored above the threshold and were
            # held back by a guard, which is a different fact from not matching.
            self.stdout.write(
                f"  ({skipped} scored above the threshold but would not be written — "
                "a link you set by hand, or one that would close a cycle.)"
            )

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
