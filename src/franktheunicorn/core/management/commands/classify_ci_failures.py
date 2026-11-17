"""Classify failing CI and draft the fix for each PR's author, from the command line.

For every open PR whose CI is red on its current head: read which job and step
failed and what the log said, decide what kind of failure it is and whose
problem, and draft the conversation comment the dashboard's CI Failures tab
shows. Nothing is posted from here — posting is a button, with the draft in
front of you.

The same work the tab's "Find failing PRs" button queues for the worker; this
runs it in the foreground, which is the right way to try it on a project for
the first time: you see each diagnosis as it lands, and can stop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from franktheunicorn.config.loader import get_operator_config, load_project_configs
from franktheunicorn.core.models import CIFailureAssessment, Project, PullRequest
from franktheunicorn.review.ci_failure import (
    classify_pr,
    findings_from,
    resolve_backend,
    run_pass,
    sync_candidates,
)

if TYPE_CHECKING:
    from django.core.management.base import CommandParser

    from franktheunicorn.config.models import ProjectConfig


class Command(BaseCommand):
    help = (
        "Classify the CI failure on open PRs and draft what to tell each author "
        "(the CI Failures queue)"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--project",
            help="Limit to one project, as owner/repo. Default is every configured project.",
        )
        parser.add_argument(
            "--pr",
            type=int,
            help=(
                "Classify this one PR number (needs --project), whatever the automatic "
                "pass's filters would say about it."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="PRs to classify per project this run (default: ci_failure.max_per_run).",
        )
        parser.add_argument(
            "--show-comment",
            action="store_true",
            help="Print each drafted comment in full.",
        )
        parser.add_argument(
            "--queue",
            action="store_true",
            help="Queue the classifications for the worker instead of running them here.",
        )

    def handle(self, *args: object, **options: object) -> None:
        wanted = str(options.get("project") or "").strip().lower()
        configs = [
            pc
            for pc in load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", ""))
            if pc.enabled and (not wanted or pc.full_name.lower() == wanted)
        ]
        if not configs:
            msg = (
                f"{wanted} is not an enabled, configured project"
                if wanted
                else "no enabled project is configured"
            )
            raise CommandError(msg)

        if options.get("queue"):
            for result in run_pass(configs, only_enabled=False):
                self.stdout.write(result.summary())
            return

        operator_config = get_operator_config()
        if resolve_backend(operator_config) is None:
            self.stderr.write(
                "No LLM backend is configured (llm_backends in operator.yaml): failures a "
                "rule explains still get a draft; the rest are listed without a diagnosis."
            )
        show_comment = bool(options.get("show_comment"))

        pr_number = options.get("pr")
        if isinstance(pr_number, int):
            if not wanted:
                raise CommandError("--pr needs --project owner/repo")
            pr = PullRequest.objects.filter(
                project__owner__iexact=configs[0].owner,
                project__repo__iexact=configs[0].repo,
                number=pr_number,
            ).first()
            if pr is None:
                msg = (
                    f"{wanted}#{pr_number} has not been ingested; look it up in the dashboard first"
                )
                raise CommandError(msg)
            self._classify(pr, show_comment)
            return

        limit_opt = options.get("limit")
        for pc in configs:
            self._run_project(pc, limit_opt if isinstance(limit_opt, int) else None, show_comment)

    def _run_project(self, pc: ProjectConfig, limit: int | None, show_comment: bool) -> None:
        project = Project.objects.filter(
            owner__iexact=pc.owner, repo__iexact=pc.repo, enabled=True
        ).first()
        if project is None:
            self.stdout.write(f"{pc.full_name}: not polled yet; nothing to classify.")
            return
        result = sync_candidates(project, pc)
        self.stdout.write(
            f"{pc.full_name}: {result.failing} PR(s) with failing CI "
            f"({result.new} newly found, {result.repinned} failing again after a push)"
        )
        rows = (
            CIFailureAssessment.objects.filter(
                pull_request__project=project,
                pull_request__state="open",
                pull_request__ci_status="fail",
                status="unclassified",
                disposition="pending",
            )
            .select_related("pull_request__project")
            .order_by("-pull_request__github_updated_at")
        )
        budget = pc.ci_failure.max_per_run if limit is None else limit
        for row in rows[: max(0, budget)]:
            self._classify(row.pull_request, show_comment)
        left = max(0, rows.count())
        if left:
            self.stdout.write(f"  {left} more left unclassified; run again, or raise --limit.")

    def _classify(self, pr: PullRequest, show_comment: bool) -> None:
        self.stdout.write(f"  {classify_pr(pr, get_operator_config())}")
        row = CIFailureAssessment.objects.filter(pull_request=pr).first()
        if row is None or not row.is_classified:
            return
        for finding in findings_from(row.failures):
            self.stdout.write(
                f"      [{finding.cause}/{finding.source}] {finding.label}: {finding.summary[:200]}"
            )
            if finding.remediation:
                self.stdout.write(f"        fix: {finding.remediation[:240]}")
        if show_comment:
            self.stdout.write("      ---- drafted comment ----")
            for line in row.comment_body.splitlines():
                self.stdout.write(f"      {line}")
