"""
Django models for franktheunicorn.

Local-first: all data lives in SQLite. No multi-tenancy, no user accounts.
These models track ingested PRs, draft reviews, anti-patterns, and operator actions.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Project(models.Model):
    """A GitHub repository the operator is monitoring."""

    owner = models.CharField(max_length=255)
    repo = models.CharField(max_length=255)
    review_context = models.TextField(default="general open-source")
    name = models.CharField(max_length=255, blank=True, default="")
    project_type = models.CharField(
        max_length=20,
        choices=[("asf", "ASF"), ("personal", "Personal"), ("org", "Organization")],
        default="personal",
    )
    config_yaml = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=True)
    repo_health_snapshot = models.JSONField(default=dict, blank=True)
    repo_health_analyzed_at = models.DateTimeField(null=True, blank=True)
    contributors_cache = models.JSONField(default=list, blank=True)
    contributors_cache_status = models.CharField(max_length=20, blank=True, default="unknown")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "repo"], name="unique_project_owner_repo"),
        ]
        ordering = ["owner", "repo"]

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @staticmethod
    def configured_q(*, lookup: str = "") -> Q:
        """Match rows whose owner/repo has an enabled YAML config."""
        from franktheunicorn.config.loader import configured_owner_repos

        keys = configured_owner_repos()
        owner = f"{lookup}__owner" if lookup else "owner"
        repo = f"{lookup}__repo" if lookup else "repo"
        if not keys:
            return Q(pk__in=())
        clause = Q()
        for owner_name, repo_name in keys:
            clause |= Q(**{f"{owner}__iexact": owner_name, f"{repo}__iexact": repo_name})
        return clause


#: The queue key for closed-but-unmerged PRs with a still-open JIRA. A module
#: constant so the dashboard, the scanner and the templates spell it once.
ZOMBIE_QUEUE = "zombie"


class PullRequest(models.Model):
    """A pull request ingested from GitHub."""

    STATE_CHOICES = [
        ("open", "Open"),
        ("closed", "Closed"),
        ("merged", "Merged"),
    ]

    QUEUE_CHOICES = [
        ("review", "Review"),
        ("intending-to-merge", "Intending to Merge"),
        ("ai-generated", "AI-Generated"),
        ("new-contributor", "New Contributor"),
        ("consider-closing", "Consider Closing"),
        ("needs-triage", "Needs Triage"),
        ("your-prs", "Your PRs"),
        ("mentioned", "Mentioned"),
        ("wip", "WIP"),
        # Closed-but-unmerged PRs whose JIRA is still open, surfaced by the
        # zombie scan (review/zombie.py). The one queue whose members are not
        # open PRs, so the inbox's state="open" filter steps aside for it.
        (ZOMBIE_QUEUE, "Zombie Queue"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="pull_requests")
    # BigInteger: GitHub's global PR ids passed 2^31 in 2024. SQLite masks the
    # overflow (dynamic 64-bit INTEGER) but Postgres rejects it on insert.
    github_id = models.BigIntegerField()
    number = models.IntegerField()
    title = models.CharField(max_length=1000)
    author = models.CharField(max_length=255)
    state = models.CharField(max_length=50, choices=STATE_CHOICES, default="open")
    url = models.URLField(max_length=1000)
    diff_url = models.URLField(max_length=1000, blank=True, default="")
    body = models.TextField(blank=True, default="")
    labels = models.JSONField(default=list, blank=True)
    requested_reviewers = models.JSONField(default=list, blank=True)
    assignees = models.JSONField(default=list, blank=True)
    changed_files = models.JSONField(default=list, blank=True)
    additions = models.IntegerField(default=0)
    deletions = models.IntegerField(default=0)

    # Scoring
    interest_score = models.FloatField(default=0.0)
    #: The un-clamped sum of score_breakdown, kept only to break ties in the
    #: dashboard ordering: interest_score saturates at 1.0 (raw / MAX_SCORE
    #: clamped), so a PR with every strong signal firing (raw well over 100)
    #: sorted identically to a merely-good one. This preserves the [0, 1]
    #: interest_score contract while letting the hottest PRs actually sort first.
    interest_score_raw = models.FloatField(default=0.0)
    score_breakdown = models.JSONField(default=dict, blank=True)

    # Flags
    is_draft = models.BooleanField(default=False)
    likely_ai_generated = models.BooleanField(default=False)
    is_operator_pr = models.BooleanField(default=False)
    is_new_contributor = models.BooleanField(default=False)
    #: GitHub's ``author_association`` for the PR author (CONTRIBUTOR, MEMBER,
    #: FIRST_TIME_CONTRIBUTOR, ...). Decides ``is_new_contributor`` when present;
    #: blank for forges and scrape rows that do not supply it.
    author_association = models.CharField(max_length=32, blank=True, default="")
    is_mentioned = models.BooleanField(default=False)
    is_low_context = models.BooleanField(default=False)
    is_likely_unowned = models.BooleanField(default=False)
    has_test_coverage = models.BooleanField(null=True, blank=True)

    # Agent session tracking (v1.25 — direct feedback channel)
    ai_agent_source = models.CharField(max_length=100, blank=True, default="")
    agent_session_url = models.URLField(max_length=1000, blank=True, default="")
    agent_task_id = models.CharField(max_length=255, blank=True, default="")

    # Merge status (fetched from single-PR endpoint)
    mergeable = models.BooleanField(null=True, blank=True)

    # Base/head SHAs from GitHub (used by blame and the differential test runner).
    base_sha = models.CharField(max_length=64, blank=True, default="")
    head_sha = models.CharField(max_length=64, blank=True, default="")
    # Head branch ref (e.g. "feature/foo"), used by the merge-queue restack
    # which needs the real branch name — PR number is not the branch name.
    head_branch = models.CharField(max_length=255, blank=True, default="")
    # Base branch ref (e.g. "master", "branch-4.0"). The CLI review tools diff
    # against the PR's base; without this they fell back to origin/main and
    # reviewed every commit on a non-main base branch that wasn't on main —
    # i.e. code that isn't in the PR.
    base_branch = models.CharField(max_length=255, blank=True, default="")

    # Cached context (v1.5)
    jira_ticket_id = models.CharField(max_length=50, blank=True, default="")
    jira_cache = models.JSONField(null=True, blank=True)
    community_context_cache = models.JSONField(null=True, blank=True)
    sentry_context_cache = models.JSONField(null=True, blank=True)

    # Queue routing (§2.2). ``queue`` is the *primary* (highest-priority) queue,
    # kept for everything that reads a single routing decision and for display.
    # ``queues`` is the full set a PR belongs to: a PR can be both "mentioned"
    # (someone @-ed the operator) and "new-contributor" at once, and the
    # dashboard surfaces it under every tab it belongs to. ``queue`` is always
    # ``queues[0]`` when any routing applies. Membership is queried with
    # :meth:`queue_filter` because SQLite has no JSON-array ``contains``.
    queue = models.CharField(max_length=50, choices=QUEUE_CHOICES, default="review")
    queues = models.JSONField(default=list, blank=True)

    # Shepherding (v2 — §2.3)
    last_shepherded_at = models.DateTimeField(null=True, blank=True)
    reviewer_comment_count = models.IntegerField(default=0)

    # Merge queue (v2)
    ci_status = models.CharField(max_length=20, blank=True, default="")
    approval_count = models.IntegerField(default=0)
    merge_queue_eligible = models.BooleanField(default=False)
    merged_at = models.DateTimeField(null=True, blank=True)
    merged_by = models.CharField(max_length=255, blank=True, default="")

    # Reviews and CI on the current head, refreshed by the poller, and the
    # "Almost Ready to Merge" verdict built from them (scoring/merge_readiness.py).
    #
    # review_states: the latest non-comment review per reviewer —
    # ``login -> {"state": "APPROVED"|"CHANGES_REQUESTED", "commit_id": sha,
    # "submitted_at": iso}``. The PR author and ``[bot]`` accounts are left out,
    # and a DISMISSED review removes the entry. approval_count above is derived
    # from this; it existed before and nothing ever wrote it.
    review_states = models.JSONField(default=dict, blank=True)
    # ci_checks: ``[{"name", "status", "conclusion", "url"}]`` for ci_checks_sha —
    # check runs and commit statuses normalised to one shape. The SHA is kept
    # because the head can move between the detail fetch and the checks fetch,
    # and learning "the rebase went green" needs to know which head it reads.
    ci_checks = models.JSONField(default=list, blank=True)
    ci_checks_sha = models.CharField(max_length=64, blank=True, default="")
    # CI-failure analysis (scoring/ci_analysis.py): why each failing check is
    # probably a flake or probably real, from signals the name-prior classifier
    # cannot see — whether the same check is failing on the base branch right
    # now, and (opt-in) an LLM reading the failure output against the diff.
    # Shape: {"sha": <head these apply to>, "base_failing": [name, ...],
    #   "verdicts": {name: {"verdict": "flake"|"real-pr"|"real-pre-existing",
    #   "reason": str, "source": "base"|"llm"}}}. SHA-guarded like ci_checks.
    ci_analysis = models.JSONField(default=dict, blank=True)
    # The readiness verdict: "ready", "flaky-ci" (ready but for a CI failure
    # that is probably a flake), or "" (not ready). The score is 0-1 like
    # interest_score; the detail JSON carries reasons, blockers and the CI
    # assessment so the dashboard can say *why* without recomputing.
    merge_readiness_score = models.FloatField(default=0.0)
    merge_readiness_verdict = models.CharField(max_length=20, blank=True, default="")
    merge_readiness_detail = models.JSONField(default=dict, blank=True)
    # The head the operator said "not ready" on. The PR stays out of the queue
    # until the author pushes again — the dismissal is about this code, not
    # the PR forever. The reason goes to MergeReadinessFeedback for learning.
    merge_readiness_dismissed_sha = models.CharField(max_length=64, blank=True, default="")
    # "Ask the author to rebase" bookkeeping: the head it was asked on, when,
    # and which checks were failing. When the head moves, the poller looks at
    # those checks again — green now is a confirmed flake, still red is a real
    # failure — and writes the answer to CICheckHistory.
    rebase_requested_sha = models.CharField(max_length=64, blank=True, default="")
    rebase_requested_at = models.DateTimeField(null=True, blank=True)
    rebase_requested_checks = models.JSONField(default=list, blank=True)

    # Intending to merge: the operator (or a configured approver) has already
    # said yes to this PR — a formal approval, or "LGTM pending CI" in a
    # comment — so it is waiting on their *merge*, not their review. Detected
    # by worker/merge_intent.py from the reviews and comments the poller
    # fetches; the approver's latest word wins, and CHANGES_REQUESTED clears
    # it. ``merge_intent_commit_sha`` is the head the approval was given
    # against, so a push since then can be shown rather than guessed.
    MERGE_INTENT_CHOICES = [
        ("", "None"),
        ("approved", "Approved"),
        ("conditional", "Conditional (pending CI)"),
    ]
    merge_intent = models.CharField(
        max_length=20, choices=MERGE_INTENT_CHOICES, blank=True, default=""
    )
    merge_intent_at = models.DateTimeField(null=True, blank=True)
    merge_intent_source = models.CharField(max_length=20, blank=True, default="")
    merge_intent_by = models.CharField(max_length=255, blank=True, default="")
    merge_intent_excerpt = models.CharField(max_length=500, blank=True, default="")
    merge_intent_commit_sha = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    github_created_at = models.DateTimeField(null=True, blank=True)
    github_updated_at = models.DateTimeField(null=True, blank=True)
    # When the poller last did the *full* per-PR refresh (files, detail,
    # comments, re-score) for this row. Distinct from updated_at, which is
    # auto_now and therefore bumped by any save at all — including the mention
    # scan's idempotent re-ingest, which would keep resetting the poller's
    # "it's been a day, re-check the time-dependent signals" escape hatch.
    last_polled_at = models.DateTimeField(null=True, blank=True)

    # Which reviewers have actually had a go at this PR: source key ->
    # ``{"at": iso8601, "status": "ok"|"failed"|"no-checkout", "findings": int,
    # "head_sha": str}``. A push sets ``stale`` so the next poll reviews the
    # new commits instead of treating the old run as current.
    #
    # Recorded rather than inferred, because inferring it from drafts cannot work:
    # a reviewer that ran and found nothing leaves exactly what a reviewer that
    # never ran leaves. That made the backfill unable to do its job — it selected
    # PRs with *no* drafts, so a PR where the LLM produced findings and the agent
    # CLI failed looked fully reviewed and was never retried, while a PR that every
    # reviewer had passed cleanly looked unreviewed forever.
    #
    # A JSONField per CLAUDE.md's "flexible structured data" convention rather than
    # a row per attempt: the read is always "which sources is this PR missing",
    # always for one PR, and the write is once per reviewer per run.
    agent_runs = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "number"], name="unique_pr_project_number"),
        ]
        ordering = ["-interest_score", "-interest_score_raw", "-github_updated_at"]

    def __str__(self) -> str:
        return f"#{self.number} {self.title}"

    @staticmethod
    def queue_filter(queue: str) -> Q:
        """A ``Q`` matching rows that belong to *queue*, SQLite-safe.

        Matches membership in the ``queues`` list (JSON array ``contains`` is
        unsupported on SQLite, so this does a text match on the quoted key),
        OR the legacy single ``queue`` field so rows routed before ``queues``
        was populated still appear under their primary tab until the next poll
        re-routes them. Queue keys come from ``QUEUE_CHOICES`` and never contain
        a double quote, so the quoted-substring match is unambiguous.
        """
        return Q(queues__icontains=f'"{queue}"') | Q(queue=queue)

    @property
    def moved_since_approval(self) -> bool:
        """New commits since the approval the merge intent records.

        Only meaningful when both SHAs are known: a row that predates the
        per-PR detail fetch has no head_sha, and "unknown" must not render as
        "pushed since you approved".
        """
        return bool(
            self.merge_intent
            and self.head_sha
            and self.merge_intent_commit_sha
            and self.head_sha != self.merge_intent_commit_sha
        )


class ReviewDraft(models.Model):
    """A draft review finding generated by the LLM (or stub).

    This is the persisted form of a ReviewFinding. The design doc calls
    the core abstraction 'ReviewFinding'; ReviewDraft is the Django model name.
    """

    CATEGORY_CHOICES = [
        ("correctness", "Correctness"),
        ("style", "Style"),
        ("security", "Security"),
        ("security-context", "Security Context"),
        ("test-coverage", "Test Coverage"),
        ("architectural", "Architectural"),
        ("naming", "Naming"),
        ("suggested-change", "Suggested Change"),
        ("moderation", "Moderation"),
        ("issue-link", "Issue Link"),
        ("backport", "Backport"),
        ("other", "Other"),
    ]

    SEVERITY_CHOICES = [
        ("critical", "Critical"),
        ("important", "Important"),
        ("nit", "Nit"),
        ("informational", "Informational"),
    ]

    pull_request = models.ForeignKey(
        PullRequest, on_delete=models.CASCADE, related_name="review_drafts"
    )
    file_path = models.CharField(max_length=1000, blank=True, default="")
    line_number = models.IntegerField(null=True, blank=True)
    line_end = models.IntegerField(null=True, blank=True)
    comment_body = models.TextField()
    suggestion = models.TextField(blank=True, default="")
    confidence = models.FloatField(
        default=0.5, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)]
    )
    sources = models.JSONField(default=list)

    # Finding metadata (§3.2)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default="other")
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default="nit")
    reasoning_trace = models.TextField(blank=True, default="")
    tone_guard_applied = models.BooleanField(default=False)
    backend_used = models.CharField(max_length=100, blank=True, default="")

    # Rejection predictor (v1.75 — Tier 2 learning)
    rejection_probability = models.FloatField(null=True, blank=True)
    is_auto_suppressed = models.BooleanField(default=False)
    code_context = models.TextField(blank=True, default="")

    DIFF_SOURCE_CHOICES = [
        ("github_api", "GitHub API"),
        ("github_scrape", "GitHub Scrape"),
        ("local_git_merged", "Local Git (Merged)"),
    ]
    diff_source = models.CharField(
        max_length=25, choices=DIFF_SOURCE_CHOICES, blank=True, default=""
    )

    # Operator disposition
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("edited", "Edited"),
        ("rejected", "Rejected"),
        ("posted", "Posted"),
        ("recalled", "Recalled"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    edited_body = models.TextField(blank=True, default="")
    rejection_reason = models.TextField(blank=True, default="")

    # Statuses counted as a "live" line-level finding for surfacing on the
    # dashboard and for the draft_findings scoring signal. Excludes "rejected"
    # (operator dismissed) and "recalled" (pulled from GitHub).
    LINE_FINDING_STATUSES: tuple[str, ...] = ("pending", "accepted", "edited", "posted")

    @classmethod
    def line_finding_q(cls) -> Q:
        """Q matching eligible line-level findings.

        Single source of truth shared by the dashboard count and the
        draft_findings scoring signal so they cannot drift.
        """
        return Q(
            line_number__isnull=False,
            is_auto_suppressed=False,
            status__in=cls.LINE_FINDING_STATUSES,
        )

    @classmethod
    def wipe_for_regenerate(cls, pull_request: PullRequest) -> int:
        """Delete non-rejected drafts on *pull_request*; return how many went.

        Rejected stays for learning. Posted forge comments are not recalled.
        """
        deleted, _ = (
            cls.objects.filter(pull_request=pull_request).exclude(status="rejected").delete()
        )
        return deleted

    # Forge posting state. The ID is whatever the source forge returned —
    # GitHub review-comment ID, Gitea pull-comment ID, GitLab note ID.
    forge_comment_id = models.BigIntegerField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Draft for {self.pull_request} @ {self.file_path}:{self.line_number}"

    @property
    def github_diff_url(self) -> str:
        """GitHub PR diff URL pointing at this finding's file and line."""
        pr_files_url = f"{self.pull_request.url}/files"
        if not self.file_path:
            return pr_files_url
        file_hash = hashlib.sha256(self.file_path.encode()).hexdigest()
        url = f"{pr_files_url}#diff-{file_hash}"
        if self.line_number:
            url += f"R{self.line_number}"
        return url


class AgentVibe(models.Model):
    """One agent/backend's overall plain-text impression of a PR.

    Sits alongside ReviewDraft rows: each backend produces an `AgentVibe`
    per PR (the overall summary) plus zero or more `ReviewDraft` rows
    (line-specific findings).
    """

    pull_request = models.ForeignKey(
        PullRequest, on_delete=models.CASCADE, related_name="agent_vibes"
    )
    backend = models.CharField(max_length=100)
    vibe_text = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["backend", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["pull_request", "backend"], name="uniq_agentvibe_pr_backend"
            )
        ]

    def __str__(self) -> str:
        return f"Vibe from {self.backend} on {self.pull_request}"


class AntiPattern(models.Model):
    """
    A pattern of review comment the operator doesn't want to make.

    Built from rejected/edited drafts. Used as a lightweight feedback signal
    to reduce pedantic or abrasive suggestions over time.
    """

    pattern_text = models.TextField()
    description = models.TextField(blank=True, default="")
    weight = models.FloatField(default=1.0, validators=[MinValueValidator(0.0)])
    times_triggered = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="anti_patterns", null=True, blank=True
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    last_matched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-weight", "-times_triggered"]

    def __str__(self) -> str:
        return f"AntiPattern: {self.pattern_text[:60]}"


class DependencyChange(models.Model):
    """A dependency version change detected in a PR, with optional changelog data.

    Created when a PR modifies dependency files (requirements.txt, pyproject.toml, etc.).
    Changelog data is fetched from PyPI + GitHub and stored for review context.
    """

    pull_request = models.ForeignKey(
        PullRequest, on_delete=models.CASCADE, related_name="dependency_changes"
    )
    package_name = models.CharField(max_length=255)
    ecosystem = models.CharField(max_length=50)  # "python", "java", "rust"
    old_version = models.CharField(max_length=100, blank=True, default="")
    new_version = models.CharField(max_length=100, blank=True, default="")
    source_file = models.CharField(max_length=500)
    changelog_url = models.URLField(max_length=1000, blank=True, default="")
    changelog_text = models.TextField(blank=True, default="")
    repository_url = models.URLField(max_length=1000, blank=True, default="")
    breaking_changes_detected = models.BooleanField(default=False)
    deprecations_detected = models.BooleanField(default=False)
    changelog_fetch_error = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [("pull_request", "package_name", "source_file")]
        ordering = ["package_name"]

    def __str__(self) -> str:
        old = self.old_version or "new"
        new = self.new_version or "removed"
        return f"{self.package_name}: {old} → {new}"


class OperatorAction(models.Model):
    """
    Log of operator actions (accept/reject/edit a draft, dismiss a PR, etc.).

    Primary feedback loop for learning what the operator values.
    """

    ACTION_CHOICES = [
        ("accept_draft", "Accept Draft"),
        ("reject_draft", "Reject Draft"),
        ("edit_draft", "Edit Draft"),
        ("dismiss_pr", "Dismiss PR"),
        ("flag_anti_pattern", "Flag Anti-Pattern"),
        # Shepherding actions (v2 — §2.3)
        ("accept_shepherd", "Accept Shepherd Draft"),
        ("reject_shepherd", "Reject Shepherd Draft"),
        ("edit_shepherd", "Edit Shepherd Draft"),
        # Almost-ready-to-merge queue actions. The reason behind a dismissal
        # lives on MergeReadinessFeedback; this is the audit trail.
        ("lgtm_pr", "LGTM (approved on the forge)"),
        ("merge_pr", "Merged PR"),
        ("dismiss_merge_candidate", "Not Ready to Merge"),
        ("request_rebase", "Asked Author to Rebase"),
        # Zombie Queue dispositions (review/zombie.py)
        ("zombie_dismiss", "Dismiss Zombie"),
        ("zombie_restore", "Restore Zombie"),
    ]
    action_type = models.CharField(max_length=50, choices=ACTION_CHOICES)
    review_draft = models.ForeignKey(
        ReviewDraft, on_delete=models.SET_NULL, null=True, blank=True, related_name="actions"
    )
    pull_request = models.ForeignKey(
        PullRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="actions"
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action_type} at {self.created_at}"


class CostRecord(models.Model):
    """Tracks LLM API token usage and estimated cost per call."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="cost_records")
    pull_request = models.ForeignKey(
        PullRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="cost_records"
    )
    action_type = models.CharField(max_length=50)  # "review", "tone-guard", "scoring"
    backend = models.CharField(max_length=100, blank=True, default="")
    tokens_in = models.IntegerField(default=0)
    tokens_out = models.IntegerField(default=0)
    # 6 decimal places so small-but-real calls (a short tone-guard rewrite,
    # a cheap local model) don't round down to $0.0000 on /stats.
    estimated_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, default=Decimal("0.000000")
    )
    duration_seconds = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action_type} ({self.backend}): ${self.estimated_cost_usd}"


class TestRun(models.Model):
    """A differential test run for a PR (§9)."""

    RUN_TYPE_CHOICES = [
        ("pr_branch", "PR Branch"),
        ("base_cherry_pick", "Base + Cherry-Picked Tests"),
    ]

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("timeout", "Timeout"),
    ]

    VERDICT_CHOICES = [
        ("good", "Good"),
        ("suspect", "Suspect"),
        ("broken", "Broken"),
        ("infra", "Infra"),
    ]

    pull_request = models.ForeignKey(
        PullRequest, on_delete=models.CASCADE, related_name="test_runs"
    )
    run_type = models.CharField(max_length=20, choices=RUN_TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    # PR head commit this run tested — the worker skips PRs whose head
    # already has a run, so unchanged PRs aren't re-tested every poll cycle.
    head_sha = models.CharField(max_length=64, blank=True, default="")
    test_scope = models.JSONField(default=list)
    results = models.JSONField(null=True, blank=True)
    differential_verdict = models.CharField(
        max_length=20, choices=VERDICT_CHOICES, null=True, blank=True
    )
    container_image = models.CharField(max_length=500, blank=True, default="")
    error_log = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        verdict = f" [{self.differential_verdict}]" if self.differential_verdict else ""
        return f"TestRun for {self.pull_request}{verdict}"


class AgentFeedback(models.Model):
    """Feedback sent to an AI agent session that created a PR (v1.25).

    Tracks structured feedback sent directly to Claude Code sessions
    or Codex tasks, as an alternative to posting GitHub comments.
    """

    ASSESSMENT_CHOICES = [
        ("good", "Good"),
        ("needs-work", "Needs Work"),
        ("reject", "Reject"),
    ]

    FEEDBACK_METHOD_CHOICES = [
        ("session-url", "Session URL"),
        ("github-comment", "GitHub Comment"),
    ]

    pull_request = models.ForeignKey(
        PullRequest, on_delete=models.CASCADE, related_name="agent_feedbacks"
    )
    assessment = models.CharField(max_length=20, choices=ASSESSMENT_CHOICES)
    feedback_body = models.TextField()
    feedback_method = models.CharField(
        max_length=20, choices=FEEDBACK_METHOD_CHOICES, default="session-url"
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Feedback for {self.pull_request}: {self.assessment}"


class LLMBackendFallback(models.Model):
    """Persisted compatibility-probe state for an LLM backend endpoint.

    ``OpenAIBackend`` discovers at runtime whether a given server accepts
    ``response_format=json_object`` and which token-count parameter name it
    requires.  Without persistence those probes repeat on every fresh
    backend instance, wasting API quota with invalid requests.

    One row per ``(provider, model, base_url)`` combination; updated in-place
    each time a fallback is activated.
    """

    provider = models.CharField(max_length=50)
    model = models.CharField(max_length=200)
    base_url = models.CharField(max_length=500, blank=True, default="")
    token_param = models.CharField(max_length=50, default="max_completion_tokens")
    supports_json_object = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "model", "base_url"],
                name="unique_llm_fallback_provider_model_base_url",
            )
        ]
        ordering = ["provider", "model"]

    def __str__(self) -> str:
        base = f" ({self.base_url})" if self.base_url else ""
        return f"LLMBackendFallback: {self.provider}/{self.model}{base}"


class MergeReadinessFeedback(models.Model):
    """What the operator said about an "Almost Ready to Merge" candidate.

    One row per button press: LGTM, merged, not ready (with a reason), or
    "asked the author to rebase". This is the learning input for the queue —
    the same role OperatorAction + AntiPattern play for findings. A dismissal
    carries a reason code and free text, plus a snapshot of the PR's feature
    tokens (author, labels, top-level paths, size bucket) so the scorer can
    later say "you've sent back 3 of 4 PRs like this" without re-deriving
    what the PR looked like at the time.
    """

    VERDICT_CHOICES = [
        ("lgtm", "LGTM"),
        ("merged", "Merged"),
        ("not-ready", "Not Ready"),
        ("rebase-requested", "Rebase Requested"),
    ]

    REASON_CHOICES = [
        ("needs-tests", "Needs tests"),
        ("needs-committer-review", "Needs a committer / domain review"),
        ("not-a-flake", "The CI failure is real"),
        ("wrong-base", "Wrong target branch / backport decision"),
        ("incomplete", "Incomplete or WIP"),
        ("design", "Design concerns"),
        ("risky", "Too risky without more eyes"),
        ("author-unresponsive", "Waiting on the author"),
        ("other", "Other"),
    ]

    pull_request = models.ForeignKey(
        PullRequest, on_delete=models.CASCADE, related_name="merge_feedback"
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="merge_feedback")
    verdict = models.CharField(max_length=20, choices=VERDICT_CHOICES)
    reason_code = models.CharField(max_length=40, choices=REASON_CHOICES, blank=True, default="")
    reason_text = models.TextField(blank=True, default="")
    head_sha = models.CharField(max_length=64, blank=True, default="")
    # {"tokens": [...], "failing_checks": [...], "ci_status": str,
    #  "approvers": [...], "readiness_score": float}
    features = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["project", "verdict"])]

    def __str__(self) -> str:
        reason = f" ({self.reason_code})" if self.reason_code else ""
        return f"{self.verdict}{reason} on {self.pull_request}"


class CICheckHistory(models.Model):
    """What the project has learned about one CI check's honesty.

    ``flaky_count`` is evidence the check flakes: the author rebased after we
    asked and it went green, or the operator marked it flaky. ``real_count``
    is evidence a failure meant something: the operator dismissed a candidate
    with "the CI failure is real", or the re-run still failed. The CI
    classifier blends these (Laplace-smoothed) with its name-based prior, so
    a check the project's config never mentioned can still earn "probably a
    flake" — and a configured-flaky one can lose it.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="ci_check_history")
    check_name = models.CharField(max_length=255)
    flaky_count = models.IntegerField(default=0)
    real_count = models.IntegerField(default=0)
    last_failed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-flaky_count", "check_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "check_name"], name="unique_ci_check_history_per_project"
            )
        ]

    def __str__(self) -> str:
        return f"{self.check_name}: flaky={self.flaky_count} real={self.real_count}"


class SecurityReport(models.Model):
    """A security vulnerability report submitted for triage.

    Supports manual paste and email ingestion. LLM-based triage assesses
    POC validity, checks for expected/documented behavior, and searches
    public CVE databases for duplicates.
    """

    STATUS_CHOICES = [
        ("new", "New"),
        ("triaging", "Triaging"),
        ("valid", "Valid"),
        ("invalid", "Invalid"),
        ("duplicate", "Duplicate"),
        ("expected-behavior", "Expected Behavior"),
        # The two verdicts between valid and invalid that a real backlog is
        # mostly made of, from Apache Magpie's triage taxonomy: the code does
        # what the report says, but the attack sits outside the security
        # model. Defense-in-depth still owes an ordinary (non-CVE) fix;
        # info-only owes an explanation and nothing else.
        ("defense-in-depth", "Defense in Depth"),
        ("info-only", "Info Only"),
    ]

    #: The machine's triage class, staged like ``auto_triage_status`` and never a
    #: verdict. Six classes plus the honest "not sure", adapted from Apache
    #: Magpie's ``security-issue-triage`` skill; see ``security/prompt.py``.
    DISPOSITION_CHOICES = [
        ("VALID", "Valid"),
        ("DEFENSE-IN-DEPTH", "Defense in depth"),
        ("INFO-ONLY", "Info only"),
        ("INVALID", "Invalid"),
        ("PROBABLE-DUP", "Probable duplicate"),
        ("FIX-ALREADY-PUBLIC", "Fix already public"),
        ("UNCERTAIN", "Uncertain"),
    ]

    SOURCE_CHOICES = [
        ("paste", "Pasted"),
        ("email", "Email"),
        ("zip", "Zip import"),
    ]
    RELAY_KIND_CHOICES = [
        ("asf-security", "ASF Security Team"),
        ("ghsa", "GitHub Security Advisory"),
    ]

    SEVERITY_CHOICES = [
        ("critical", "Critical"),
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
        ("informational", "Informational"),
        ("unknown", "Unknown"),
    ]

    #: A list/export filter that is not a status: rows carrying a CVE id with no
    #: branch recorded as fixing them. It lives on the model, next to
    #: STATUS_CHOICES, because the dashboard tab and the CSV export both have to
    #: mean the same thing by it — the export honours whichever filter the page is
    #: showing, so a filter the page understands and the export doesn't is a 400 on
    #: the Export button.
    CVE_NO_BRANCH_FILTER = "cve-no-branch"

    #: The other cross-cutting filter: reports ruled valid but with no CVE id yet.
    #: The worklist for "confirmed real, not yet CVE'd" — the queue an operator
    #: works through to push for CVE assignment. Lives on the model for the same
    #: reason ``CVE_NO_BRANCH_FILTER`` does: the tab and the export have to mean
    #: the same thing by it, or the Export button 400s on a tab the page offered.
    VALID_NO_CVE_FILTER = "valid-no-cve"

    #: Statuses that owe no fix, so no branch is the answer rather than a gap.
    #: ``duplicate`` is here because that is what ``matched_cve_id`` was built for —
    #: without it the queue on a real backlog is almost entirely "dup of CVE-X", and
    #: a queue you have to skim past is one nobody opens twice.
    #:
    #: A tuple for a stable order in the emitted parameters and in anything that
    #: prints them; the SQL text is identical either way, since ``IN`` values are
    #: bound parameters rather than inlined.
    NO_FIX_OWED_STATUSES = ("invalid", "expected-behavior", "duplicate", "info-only")

    @classmethod
    def list_filters(cls) -> set[str]:
        """Every legal value of the list page's ``?status=`` and the export's
        ``--status``, statuses plus the cross-cutting filters.

        One definition because the dashboard, the export view and
        ``export_security_csv`` each validate the same parameter, and the two that
        hand-copied the set are the two that would disagree after the next filter.
        """
        return {key for key, _ in cls.STATUS_CHOICES} | {
            cls.CVE_NO_BRANCH_FILTER,
            cls.VALID_NO_CVE_FILTER,
        }

    @staticmethod
    def confirmed_branch_q() -> Q:
        """Match rows whose ``fixed_in_branch`` somebody actually vouched for.

        ``branch_scan`` writes that column itself when a branch name contains the
        report's CVE id, so "the field is non-empty" stopped meaning "a human
        settled this". Three readers mean the second thing and each spelled the
        first: :meth:`operator_has_ruled_q`, the expensive version-map/verifier
        fan-out in the dashboard and the worker, and
        :meth:`cve_without_branch_q`. Spelled once, because the next reader will
        get it wrong the same way — the two that already had were both "skip the
        expensive work, the operator has an answer" when nobody had answered.
        """
        return ~Q(fixed_in_branch="") & Q(branch_match_applied=False)

    @property
    def has_confirmed_branch(self) -> bool:
        """:meth:`confirmed_branch_q`, for one instance."""
        return bool(self.fixed_in_branch and not self.branch_match_applied)

    @staticmethod
    def operator_has_ruled_q() -> Q:
        """:attr:`operator_has_ruled`, for a queryset.

        A property can't be used in a filter, so the selectors that pick reports for
        bulk work were testing it in Python against a list materialised before the
        loop. That reintroduces the race the close path already re-reads status to
        avoid: a run over a few thousand reports takes real wall-clock time, and a
        verdict typed at minute three is invisible to a snapshot taken at minute zero.
        """
        return ~Q(operator_notes="") | ~Q(matched_cve_id="") | SecurityReport.confirmed_branch_q()

    @property
    def operator_has_ruled(self) -> bool:
        """Whether the operator has put anything of their own on this report.

        The bulk paths — re-run triage, re-run procedural — use this to leave
        already-worked reports alone, and each of them used to spell the test out
        inline. Spelled once because every new operator-owned field has to join it:
        ``fixed_in_branch`` did not, and a report with a branch recorded was re-billed
        for two LLM calls and had a fresh suggestion staged over the operator's work.

        ``branch_match_applied`` is the exception, and it is load-bearing:
        :mod:`franktheunicorn.security.branch_scan` writes ``fixed_in_branch``
        itself when a branch name contains the report's CVE id. That is the
        machine's answer, not the operator's, so counting it here made a git sweep
        silently retire an untriaged report from every bulk path *and* made the
        procedural-close flash report it under "skipped (operator-ruled: notes, a
        CVE, or a fix branch)" — a false statement about a report nobody had
        looked at.
        """
        return bool(self.operator_notes or self.matched_cve_id or self.has_confirmed_branch)

    @staticmethod
    def cve_without_branch_q() -> Q:
        """Match reports with a CVE id and nothing recorded as fixing it.

        ``__gt=""`` rather than ``~Q(matched_cve_id="")``: for a non-null CharField
        the two select the same rows, but ``NOT (col = '')`` is not answerable from
        an index, so the negated form quietly makes the predicate unindexable —
        measured as a full SCAN with an index present where ``> ''`` SEARCHes it.
        Nothing indexes this column today; the point is that whoever adds the index
        to fix a slow queue gets the speedup instead of concluding it didn't help.
        The status clause stays negated because there is no index to lose there and
        no useful one to add — five values over the whole table.

        ``duplicate_of`` as well as the ``duplicate`` status, because
        :mod:`franktheunicorn.security.duplicates` links without judging and
        deliberately never sets that status — so a backlog-detected duplicate is
        still ``new``/``valid``, and six findings of one missing check in a shared
        helper would be six rows in the queue.

        "No branch" means no *confirmed* branch — a machine tie from
        ``branch_scan`` stays in the queue. This is a worklist and a count the
        operator works through, so retiring rows on evidence this same class says
        is not a ruling would silently shrink it on the machine's word. The tie is
        still worth having here: the row renders the branch and the reason that
        found it, so confirming one is a glance rather than git archaeology, which
        was the point of the sweep. ``recheck.untriaged_by_project`` draws the
        line the other way on purpose — that one is deciding whether to spend a
        cloud agent, not what the operator has to look at.

        ``fix_branch_primary`` counts here too: it is the scan's own answer to
        "which branch carries the fix", imported through the review sheet by the
        operator, so a non-empty value is a branch the operator endorsed by
        importing — not a machine tie like ``branch_match_applied``. Without this,
        a report the scan found a branch for stayed in the "no branch" queue on the
        strength of an empty ``fixed_in_branch``, which is the field the operator
        types but not the only one that records a branch. The "(none)" sentinel the
        scan emits for "no branch" is normalised to empty on import, so it doesn't
        read as a branch name.
        """
        return (
            Q(matched_cve_id__gt="")
            & ~SecurityReport.confirmed_branch_q()
            & Q(fix_branch_primary="")
            & Q(duplicate_of__isnull=True)
            & ~Q(status__in=SecurityReport.NO_FIX_OWED_STATUSES)
        )

    @staticmethod
    def valid_without_cve_q() -> Q:
        """Match reports ruled valid but carrying no CVE id.

        The "confirmed real, not yet CVE'd" worklist — the queue an operator works
        through to push for CVE assignment. ``status="valid"`` is the operator's
        ruling that the report is a real hole, and ``matched_cve_id=""`` is the gap
        this tab exists to surface. ``__gt=""`` is not used here because the empty
        case is the one we want, and there is no index on this column to lose by
        negating; the ``status="valid"`` half is the selective one anyway.
        """
        return Q(status="valid") & Q(matched_cve_id="")

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="security_reports",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=500, blank=True, default="")
    raw_text = models.TextField()
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="paste")
    reporter_name = models.CharField(max_length=255, blank=True, default="")
    reporter_email = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new")

    # The machine's suggested verdict, staged rather than applied. Triage
    # writes this — "valid", "invalid", "expected-behavior", or blank for
    # inconclusive — and leaves ``status`` alone, so ``status`` is the
    # operator's field and theirs alone: a non-auto status is unambiguously a
    # ruling, not a machine claim the operator never saw. The detail page
    # shows the suggestion with an Agree button that copies it into
    # ``status``. Blank means "triage ran but the model didn't commit" or
    # "never triaged"; the two are told apart by whether the assessment
    # fields below are populated.
    auto_triage_status = models.CharField(max_length=20, blank=True, default="")
    #: The class behind that suggestion, kept even where statuses collapse it:
    #: PROBABLE-DUP and FIX-ALREADY-PUBLIC both end in no CVE for this report,
    #: but the operator's next step differs (link the duplicate / confirm the
    #: public fix covers it). Blank when the model did not say.
    auto_triage_disposition = models.CharField(
        max_length=24, blank=True, default="", choices=DISPOSITION_CHOICES
    )
    #: The one line of the security model or project context the model says
    #: its disposition rests on, verbatim. Empty when it cited nothing, which
    #: is itself worth seeing: a confident class with no quoted rule is a guess.
    triage_model_quote = models.TextField(blank=True, default="")
    #: Prompt-injection patterns in the report's text (title, raw text, parsed
    #: POC), by name, written by ``security.intake.record_injection_hits`` the
    #: moment the row exists and again once triage fills the POC. Empty is
    #: clean. The verifier and fix agent keep their own per-launch copies
    #: (``fix_injection_hits``); this one is the callout above the report
    #: text, there before anyone clicks anything, and the thing to read when a
    #: later verdict on this report looks steered.
    injection_hits = models.JSONField(default=list, blank=True)

    # LLM-parsed structured fields (populated by triage pipeline)
    parsed_component = models.CharField(max_length=500, blank=True, default="")
    parsed_poc = models.TextField(blank=True, default="")
    parsed_impact = models.TextField(blank=True, default="")
    #: The severity the *reporter* claims, as the parse step read it off the
    #: report. Attacker-supplied and routinely inflated, so it is context and
    #: never a rating: it goes to the verifier labelled as the reporter's, and
    #: the page shows it as "reporter claims". ``assessed_severity`` below is
    #: frank's analysis or the operator's ruling and nothing else. Apache
    #: Magpie's rule, "treat every reporter-supplied CVSS score as informational
    #: background only" (``AGENTS.md`` at apache/magpie commit
    #: dadb315caa8b881fc1d2195bef03223dac25915a).
    claimed_severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default="unknown")
    #: frank's rating: written by the triage analysis step, the verdict form or
    #: the sheet import. Until migration 0062 the parse step wrote the reporter's
    #: claim here too; rows from before that keep whatever they had.
    assessed_severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default="unknown")

    # LLM triage analysis
    triage_summary = models.TextField(blank=True, default="")
    is_expected_behavior = models.BooleanField(default=False)
    expected_behavior_explanation = models.TextField(blank=True, default="")
    poc_assessment = models.TextField(blank=True, default="")
    poc_plausible = models.BooleanField(null=True, blank=True)

    # Sandbox execution (optional follow-up)
    sandbox_requested = models.BooleanField(default=False)
    sandbox_result = models.TextField(blank=True, default="")
    sandbox_verdict = models.CharField(max_length=20, blank=True, default="")

    # Exploit verification (optional follow-up). A coding agent attempts to
    # reproduce the exploit — set up the preconditions, run the PoC, observe.
    # Sibling of the sandbox (which runs parsed_poc as a bare script) and the
    # deep verifier (which reads the code without running anything). Driven by
    # the "Attempt to verify exploit" button or --verify-exploit at import.
    exploit_requested = models.BooleanField(default=False)
    exploit_verdict = models.CharField(max_length=20, blank=True, default="")
    exploit_result = models.TextField(blank=True, default="")
    exploit_checked_at = models.DateTimeField(null=True, blank=True)

    # CVE dedup — against the *public* record.
    cve_matches = models.JSONField(default=list, blank=True)
    matched_cve_id = models.CharField(max_length=50, blank=True, default="")

    # State of the CVE record at the ASF CVE process service
    # (cveprocess.apache.org), when matched_cve_id is one of theirs and
    # somebody has pressed the button to look. Written by
    # security.cve_api.fetch_record via the dashboard — never inferred from
    # NVD, whose "status" means something else entirely.
    #: RESERVED / PUBLISHED / REJECT track the record's lifecycle; "unknown"
    #: means the service answered but said nothing recognisable (the API is
    #: young — see the unknown-response logging in security.cve_api).
    CVE_RECORD_STATE_CHOICES = [
        ("", "Never checked"),
        ("RESERVED", "Reserved"),
        ("PUBLISHED", "Published"),
        ("REJECT", "Rejected"),
        ("DRAFT", "Draft (ASF workflow)"),
        ("REVIEW", "In review (ASF workflow)"),
        ("READY", "Ready to publish (ASF workflow)"),
        ("PUBLIC", "Public (sent to cve.org)"),
        ("unknown", "Unrecognised response"),
    ]
    cve_record_state = models.CharField(
        max_length=20, blank=True, default="", choices=CVE_RECORD_STATE_CHOICES
    )
    cve_record_checked_at = models.DateTimeField(null=True, blank=True)

    # Dedup against your own backlog, which is a different question and the one that
    # bites at volume. A scanner archive emits one finding per site, so a missing
    # check in a shared helper arrives as six findings against six callers; two scans
    # of the same repo duplicate everything; a public disclosure gets forwarded by
    # three people. Working through several hundred of those, the expensive mistake
    # isn't mis-triaging one report, it's investigating the same hole four times —
    # or fixing it once and leaving three open rows that read as live vulnerabilities.
    #
    # SET_NULL rather than CASCADE: deleting the report you kept must not delete the
    # duplicates pointing at it. They'd be the only remaining record of the finding.
    duplicate_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicates",
    )
    duplicate_confidence = models.FloatField(null=True, blank=True)
    # What pushed it over the threshold, in words. A bare 0.72 is not something an
    # operator can check, and a heuristic link is only worth having if it gets
    # checked. See security.duplicates.score_pair for what goes in here.
    duplicate_reason = models.CharField(max_length=500, blank=True, default="")

    # Operator verdict
    operator_notes = models.TextField(blank=True, default="")

    # What somebody who isn't the operator said about this report. A security
    # backlog gets ruled on by more people than the one maintainer running this
    # install — on an ASF project that's the PMC — and they are not going to be
    # handed a Tailscale address and a login. So the collaboration path is a CSV
    # round-trip through a shared spreadsheet (see security.sheet_sync), and this
    # is the column they write in.
    #
    # Deliberately not operator_notes: merging two people's notes into one field
    # means the next export can't tell whose text it's sending back out, and an
    # import silently overwrites whichever was edited more recently. Two fields
    # cost a migration; one field costs somebody's review comment.
    external_notes = models.TextField(blank=True, default="")
    # When external_notes last actually changed, not when the row was last
    # imported — a re-import of an unedited sheet must not make stale comments
    # look fresh, which is the whole reason this isn't just updated_at.
    external_notes_at = models.DateTimeField(null=True, blank=True)

    # A scanner's own id for this finding (e.g. "f001"), when the report came out
    # of a findings manifest rather than a standalone file. Kept because it's the
    # join key the archive itself uses: PATCHES/bug_N/meta.json names the finding
    # its patch fixes, which is how proposed_patch below gets attached.
    finding_id = models.CharField(max_length=100, blank=True, default="")

    # A proposed fix shipped alongside the finding. Scanner archives carry one
    # patch per finding and they're the most directly actionable thing in the
    # bundle, so they're kept and shown rather than skipped as "not a report".
    proposed_patch = models.TextField(blank=True, default="")
    proposed_patch_path = models.CharField(max_length=500, blank=True, default="")

    # When the vulnerable code was introduced, from git history alone. One answer
    # per report rather than per branch — unlike "is it there", "when did it get
    # there" has a single answer, and the release tags containing that commit are
    # a definitive affected-versions list where file presence is circumstantial.
    introduced_commit = models.CharField(max_length=64, blank=True, default="")
    introduced_at = models.DateTimeField(null=True, blank=True)
    #: ``patch-line`` (pickaxed the removed lines, a real answer) or
    #: ``file-added`` (only when the file appeared, a floor). Kept because the
    #: two are worth very different amounts and collapsing them hides which.
    introduced_method = models.CharField(max_length=20, blank=True, default="")
    #: Release tags containing :attr:`introduced_commit`, oldest first, capped —
    #: a 2014 Spark commit is in 86 releases and the list is not the answer.
    introduced_releases = models.JSONField(default=list, blank=True)
    #: How many releases contain it, before that cap.
    introduced_release_count = models.IntegerField(default=0)
    introduced_summary = models.TextField(blank=True, default="")

    # The operator's canonical affected-versions answer — the sentence that
    # goes in an advisory. Distinct from the machine-gathered evidence
    # (verifier/version-map branch rows, the introduction scan's release list)
    # on purpose: those are inputs, this is the verdict, and a verdict
    # somebody can act on has to be one field the operator owns. The detail
    # page offers a copy button to seed it from the machine's rollup so the
    # operator doesn't retype what the agent already worked out.
    affected_versions = models.TextField(blank=True, default="")

    # The one-click fix agent (security.fix_agent): a button press launches a
    # Cursor cloud agent that applies the scanner's patch, scrubs the wording
    # so nothing about the branch or comments says "security", and pushes an
    # innocuously-named branch to the operator's fork. These track that:
    # which branch the fix is based on (master vs branch-3.5, read off the
    # archive name), the branch the agent pushed, and the Cursor agent/run ids
    # so a refresh can ask the API where the run got to.
    fix_base_branch = models.CharField(max_length=100, blank=True, default="")
    fix_branch = models.CharField(max_length=200, blank=True, default="")
    #: Tip of ``fix_branch`` when it was last seen, which is how a relaunch tells
    #: "the branch we gave up on" from "that name, repushed by this run".
    fix_branch_sha = models.CharField(max_length=64, blank=True, default="")
    #: ``[{"branch": ..., "sha": ...}]`` abandoned by a relaunch. The refresh asks
    #: the fork rather than this row, so an abandoned branch comes straight back
    #: and gets reported as the new run's result unless it is remembered here.
    fix_superseded = models.JSONField(default=list, blank=True)
    fix_agent_id = models.CharField(max_length=100, blank=True, default="")
    fix_run_id = models.CharField(max_length=100, blank=True, default="")
    #: "" (never launched), "launched", "branch-pushed", "finished-no-branch"
    #: (terminal: the run ended with no branch naming the finding), "failed".
    fix_status = models.CharField(max_length=20, blank=True, default="")
    fix_status_detail = models.CharField(max_length=300, blank=True, default="")
    #: Prompt-injection patterns the report tripped at launch. Its own field
    #: because ``refuse_on_injection`` defaults False and "recorded on the row"
    #: is then the whole mitigation — so it has to reach the operator in every
    #: state, not just the failed one, next to the branch they are about to judge.
    fix_injection_hits = models.JSONField(default=list, blank=True)
    #: Security-saying wording found on the fix branch name(s) by
    #: ``security.fix_language.scan_branches`` at the last refresh: a CVE id, a
    #: phrase like "sql injection", or the report's own identifiers. Empty is
    #: clean. Never a block (frank does not open the PR), always a red badge:
    #: the branch list on a public fork is readable before the advisory is.
    fix_language_hits = models.JSONField(default=list, blank=True)
    fix_launched_at = models.DateTimeField(null=True, blank=True)

    # Which branch actually carries the fix — the operator's answer, the way
    # affected_versions is the operator's answer about versions. Deliberately not
    # fix_branch above: that one is the cloud agent's output, a proposal nobody has
    # ruled on yet, and it gets overwritten by the next relaunch. This is the
    # operator saying "the fix for this lives here", which is the thing you need
    # when a CVE id has been assigned and somebody asks what ships it.
    #
    # Free text rather than a FK to anything, because the branch is frequently not
    # in a repo this install tracks (a private staging fork, an embargoed branch)
    # and a fix that lands on three release branches is written as a list.
    # ``cve_without_branch_q`` only ever asks whether it is empty.
    #
    # TextField, like affected_versions above: a realistic answer is
    # "master, branch-4.0, branch-3.5 (backport pending)", which ran past the 200
    # the column started with. That bound bought nothing and cost four copies of
    # the number — model, migration, view check, and a maxlength the widened column
    # would not have followed — plus a rejection htmx never showed the operator.
    fixed_in_branch = models.TextField(blank=True, default="")

    # Where the fix actually lives, from a tool that walked origin's branches and
    # matched the report's CVE id / finding id against branch names and commit
    # messages (see NOTES in the scan-output tree). Three columns that tool emits,
    # imported through the review sheet rather than recomputed here: the walk is a
    # fetch plus two git logs per branch and belongs on a box with the checkout,
    # not in the request that serves the dashboard.
    #
    # Writable through the sheet on purpose — the whole point of carrying them is
    # that a CSV with these columns (the "with fix branches" export a PMC reviews)
    # round-trips: import populates them, export sends them back out, and a column
    # the reviewer deletes on the way back is "no instruction", never "blank it"
    # (see sheet_sync._proposed_changes). Distinct from fixed_in_branch above,
    # which is the operator's own one-line answer; these are the machine's evidence
    # for it, kept separately so the operator's verdict and the scan's finding can
    # disagree and both be visible.
    fix_branch_primary = models.CharField(max_length=300, blank=True, default="")
    # Semicolon-separated, rollups annotated "(rollup)". TextField because a fix
    # that landed on every release line is a long list and a CharField bound
    # would just truncate it silently.
    fix_branch_all = models.TextField(blank=True, default="")
    #: Whether the finding's own fix commit (by git patch-id) has landed upstream.
    #: Survives Spark's squash-merge, where the merged SHA differs from the branch
    #: tip. Empty means "the scan never ran"; ``no-fix-branch`` is the scan's
    #: answer for a report it had no branch to check.
    FIX_MERGED_CHOICES = [
        ("", "Never checked"),
        ("merged:master+branch-4.x", "Merged into master and branch-4.x"),
        ("merged:branch-4.x", "Merged into branch-4.x only"),
        ("not-merged", "Not merged upstream"),
        ("no-fix-branch", "No fix branch found"),
    ]
    fix_merged_upstream = models.CharField(
        max_length=40, blank=True, default="", choices=FIX_MERGED_CHOICES
    )

    # "Has the fix landed upstream?" — the machine's answer, computed by
    # security.fix_landed (git ancestry + tag checks, with a cloud-agent
    # fallback for what git can't prove). Deliberately separate from
    # fix_merged_upstream above: that column round-trips through the review
    # sheet and is the PMC's/operator's ruling, these are the evidence —
    # the same split as fixed_in_branch vs branch_match_*. A machine verdict
    # here never overwrites the sheet's column.
    #: "merged" means an ancestor of the default branch; "released" means a
    #: release tag contains it. "indeterminate" covers everything git could
    #: not prove — including the degenerate case where fixed_in_branch *is* a
    #: mainline branch and there is no topic ref to test.
    FIX_LANDED_STATUS_CHOICES = [
        ("", "Never checked"),
        ("merged", "Merged upstream"),
        ("released", "In a release tag"),
        ("not-merged", "Not merged upstream"),
        ("no-fix-branch", "No fix branch to check"),
        ("indeterminate", "Git could not prove it either way"),
    ]
    fix_landed_status = models.CharField(
        max_length=20, blank=True, default="", choices=FIX_LANDED_STATUS_CHOICES
    )
    #: What the check actually saw: {"checked_ref", "branches", "tags",
    #: "pr_url", "pr_merged_at"}. The reason an unaccountable badge is a badge
    #: nobody trusts — same argument as branch_match_reason.
    fix_landed_detail = models.JSONField(default=dict, blank=True)
    fix_landed_checked_at = models.DateTimeField(null=True, blank=True)
    #: "git" (ancestry/tags — proof) or "agent" (a cloud agent read the log —
    #: a judgement call). Not collapsed into the detail JSON, for the
    #: recheck_method reason: the two answers are worth different amounts.
    FIX_LANDED_METHOD_CHOICES = [
        ("", "Never checked"),
        ("git", "Git ancestry and tag checks"),
        ("agent", "Cloud agent read the commit log"),
    ]
    fix_landed_method = models.CharField(
        max_length=20, blank=True, default="", choices=FIX_LANDED_METHOD_CHOICES
    )

    # The batch "did the last month of commits fix this?" recheck. Two things
    # write it, because it is one question: security.recheck puts a cloud agent
    # on a month of commit log, and security.branch_scan reverse-applies the
    # report's own proposed patch. The second is proof where the first is a
    # judgement call, so which one answered is worth keeping — see
    # recheck_method. "" is "never checked".
    #: Declared rather than left to three modules and two templates to agree
    #: about by hand, which is the house pattern next door (``STATUS_CHOICES``)
    #: and which this column had outgrown: ``branch_scan`` names three values,
    #: ``recheck._VERDICTS`` names two of them, and the templates carried all four
    #: as literals. A fifth added by either writer renders as the detail page's
    #: ``{% else %}`` badge — "still plausibly valid" beside a reason saying
    #: something else, the contradiction the ``unclear`` branch was just added to
    #: stop.
    RECHECK_STATUS_CHOICES = [
        ("", "Never checked"),
        ("still-valid", "Still valid"),
        ("likely-fixed", "Likely fixed already"),
        # git-only: the patch neither applies nor reverse-applies, so the code
        # moved and nothing cheap can say whether it moved because of this.
        ("unclear", "The code moved — unclear"),
    ]

    #: Which subsystem answered. See :attr:`recheck_method`.
    RECHECK_METHOD_CHOICES = [
        ("", "Never checked"),
        ("agent", "Cloud agent read the commit log"),
        ("git", "Reverse-applied the proposed patch"),
    ]

    recheck_status = models.CharField(
        max_length=20, blank=True, default="", choices=RECHECK_STATUS_CHOICES
    )
    recheck_reason = models.TextField(blank=True, default="")
    #: "" (never checked) / "agent" / "git". Not collapsed into recheck_reason,
    #: for the same reason introduced_method isn't collapsed into
    #: introduced_summary: the two answers are worth very different amounts and
    #: a column that hides which is a column nobody can act on.
    recheck_method = models.CharField(
        max_length=20, blank=True, default="", choices=RECHECK_METHOD_CHOICES
    )
    rechecked_at = models.DateTimeField(null=True, blank=True)

    # Which branch on origin looks like it carries the fix, from git alone
    # (security.branch_scan). Distinct from fix_branch above, which is what
    # frank's own cloud agent pushed, and from fixed_in_branch below, which is
    # the operator's answer: this is a *scan* of every branch origin has,
    # matching CVE ids and finding ids in branch names and commit messages.
    #
    # A confident match is copied into fixed_in_branch and branch_match_applied
    # records that we did it — that is the "tie the branches in automatically"
    # the sweep exists for. A softer one stays here alone, where the list page
    # offers it and the operator can ignore it. Either way the reason is stored,
    # because a bare branch name with a 0.85 next to it is not something anybody
    # can check, and an unheld heuristic link is worse than none.
    branch_match_branch = models.CharField(max_length=200, blank=True, default="")
    branch_match_confidence = models.FloatField(null=True, blank=True)
    branch_match_reason = models.CharField(max_length=500, blank=True, default="")
    branch_match_applied = models.BooleanField(default=False)
    branch_matched_at = models.DateTimeField(null=True, blank=True)

    # Build dependencies between findings of the same archive, parsed from the
    # scanner's own composition notes ("bug_86 depends on bug_83's conf —
    # cherry-picking one without the other does not compile"). Not duplication:
    # both are real findings, one just doesn't apply without the other, which
    # the fix agent needs to know before it bases a branch on thin air.
    depends_on = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="required_by",
    )

    # How far up the queue this report belongs, higher first. Read off a scanner
    # archive's own ranking at import (severity tier, triage-panel verdict, panel
    # confidence, CVSS overlay) — see security.scan_archive._priority. Zero for
    # everything that arrived without a ranking, which is every pasted and emailed
    # report, so it sorts below anything a scanner actually rated. The reason is
    # stored alongside because an unaccountable number is a number nobody trusts.
    priority = models.FloatField(default=0.0, db_index=True)
    priority_reason = models.CharField(max_length=200, blank=True, default="")

    # Which archive a source="zip" report came from. Two scans of the same repo
    # contain the same entry paths with different contents, so the path alone
    # doesn't identify a report: importing three real scanner archives produced
    # 23 pairs of identically-titled rows with no way to tell which scan each came
    # from. Provenance, not decoration — a finding is only actionable if you know
    # what was scanned.
    source_archive = models.CharField(max_length=255, blank=True, default="")

    # Which automated agent produced this report, or "" for one a person sent.
    # Orthogonal to ``source``, which is the *door* (paste, email, zip): another
    # agent's findings arrive through all three, and a human's backlog arrives as
    # a zip too. Named by the operator at import — no archive we've seen says who
    # wrote it, and guessing from a manifest's shape is wrong exactly when it
    # matters. Normalised by ``security.agent_source.clean_agent_name`` so
    # "Claude Code" and "claude-code" are one filter, not two. Indexed because
    # every bulk button and the list's source row filter on it.
    source_agent = models.CharField(max_length=100, blank=True, default="", db_index=True)

    # Email metadata (populated when source=email)
    email_message_id = models.CharField(max_length=500, blank=True, default="")
    email_received_at = models.DateTimeField(null=True, blank=True)
    #: How the report reached the inbox when a relay forwarded it: the ASF
    #: Security Team's "Dear PMC" template, or a GitHub Security Advisory
    #: notification. Blank when the reporter wrote directly or the parser could
    #: not tell. The sender of a relayed mail is the broker, never the reporter,
    #: which is what the next two fields are for.
    relay_kind = models.CharField(max_length=20, blank=True, default="", choices=RELAY_KIND_CHOICES)
    #: The name the relay's credit line gives, verbatim ("This vulnerability was
    #: discovered and reported by ..."). What the CVE record's credits should
    #: say; ``reporter_name`` is who the mail came from.
    reporter_credit = models.CharField(max_length=255, blank=True, default="")
    #: "human", "tool" or "service" for a credited name, from Magpie's kind
    #: hints. A tool is credited in CVE 5 as type "tool", not as a finder.
    credit_kind = models.CharField(max_length=10, blank=True, default="")
    #: ``[{"filename", "content_type", "size_bytes", "sha256"}]`` for every MIME
    #: attachment on an emailed report. Data about the attachment, never its
    #: bytes: nothing is extracted or executed. The click-gated sandbox runs
    #: text the operator pasted into it, and only that.
    attachments = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"SecurityReport: {self.title or self.raw_text[:60]}"


class SecuritySheetSnapshot(models.Model):
    """One report's writable state, frozen just before a sheet import overwrote it.

    The undo for the review-sheet round trip. An import is one transaction that
    can touch hundreds of rows, and a PMC's edits landing on top of an operator's
    later ruling is exactly the clobber the staleness guard exists to refuse —
    but ``--force`` / "let the sheet win" is a real, used path, and a misclick
    there used to be unrecoverable short of a backup restore. So: before every
    applied import, snapshot the writable fields of every report it changed, tag
    them with the import's id, and keep only the latest import's set. One undo
    restores that set; there is no redo and no second level back, which is enough
    for "I imported the wrong sheet" and honest about being a safety net rather
    than version control.

    Only the fields :mod:`franktheunicorn.security.sheet_sync` can write, so the
    snapshot is exactly what an undo can put back. A worker triage run or a
    dashboard verdict typed between the export and the import is not reverted —
    those touch fields the sheet never owns, and the snapshot does not store them.
    """

    #: Groups the rows from one import. A fresh uuid per import; the older set is
    #: deleted when a new import lands, so the table holds at most one import's
    #: worth of rows.
    import_id = models.CharField(max_length=36, db_index=True)
    report = models.ForeignKey(
        SecurityReport,
        on_delete=models.CASCADE,
        related_name="sheet_snapshots",
    )
    # The writable set, copied verbatim before the import's writes. Kept as real
    # columns rather than a JSON blob so a future "what did the last import change?"
    # view can query them without unpacking, and so a column added to
    # WRITABLE_COLUMNS is a column that has to be added here too — the migration
    # makes the omission loud, where a JSON blob would drop it silently.
    status = models.CharField(max_length=20, blank=True, default="")
    assessed_severity = models.CharField(max_length=20, blank=True, default="")
    matched_cve_id = models.CharField(max_length=50, blank=True, default="")
    external_notes = models.TextField(blank=True, default="")
    operator_notes = models.TextField(blank=True, default="")
    external_notes_at = models.DateTimeField(null=True, blank=True)
    fix_branch_primary = models.CharField(max_length=300, blank=True, default="")
    fix_branch_all = models.TextField(blank=True, default="")
    fix_merged_upstream = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One snapshot per report per import. The importer writes a snapshot
            # before each apply, and a sheet with two rows for one report (caught
            # later as "duplicate-row") would otherwise leave two snapshots and
            # an undo would restore the older one first for no reason.
            models.UniqueConstraint(
                fields=["import_id", "report"],
                name="unique_sheet_snapshot_per_import_report",
            ),
        ]

    def __str__(self) -> str:
        return f"SecuritySheetSnapshot(import={self.import_id[:8]}, report={self.report_id})"


class SecurityRecheckRun(models.Model):
    """One batch "did recent commits fix these?" agent run, scoped to a project.

    The launch is one POST; the run itself takes minutes on Cursor's infra, so
    the ids live here and a worker command (``poll_security_rechecks``) does the
    waiting and writes the per-report verdicts (``SecurityReport.recheck_*``)
    when the run finishes. One row per project per batch — the agent clones one
    repo, so a backlog spanning two projects is two runs.
    """

    STATUS_CHOICES = [
        ("launched", "Launched"),
        ("finished", "Finished"),
        ("error", "Error"),
    ]

    #: What the run was launched to answer. "recheck" is the original batch
    #: question (did the last month of commits fix these? writes
    #: ``SecurityReport.recheck_*``); "fix-landed" is the fallback for reports
    #: git could not prove landed or not (writes ``SecurityReport.fix_landed_*``
    #: with ``fix_landed_method="agent"``); "valid-check" is the per-report
    #: fan-out over the triaged-real backlog (one run per report, asking "is
    #: this issue fixed yet?", writing ``SecurityReport.recheck_*``). One model
    #: for all three because the lifecycle — one POST, poll, write per-report
    #: verdicts — is identical; the kind is in the uniqueness constraint so the
    #: three never block each other. For "valid-check" the chunk_index carries
    #: the report pk: the constraint then dedups a double-press per report.
    KIND_RECHECK = "recheck"
    KIND_FIX_LANDED = "fix-landed"
    KIND_VALID_CHECK = "valid-check"
    KIND_CHOICES = [
        (KIND_RECHECK, "Did recent commits fix these?"),
        (KIND_FIX_LANDED, "Did the known fix land upstream?"),
        (KIND_VALID_CHECK, "Is this valid report fixed yet?"),
    ]
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_RECHECK)

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="security_recheck_runs",
    )
    agent_id = models.CharField(max_length=100, blank=True, default="")
    run_id = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="launched")
    #: How many reports the run was asked about.
    report_count = models.IntegerField(default=0)
    #: Which slice of the project's backlog this run covers. A backlog past
    #: ``recheck._MAX_REPORTS_PER_RUN`` is several runs, so it is this — not the
    #: project alone — that identifies a run within a batch, and it is what makes
    #: the uniqueness constraint below expressible.
    chunk_index = models.IntegerField(default=0)
    #: The security list's source scope the run was launched under ("all", "none",
    #: "any" or an agent name — see ``security.agent_source``). Recorded because a
    #: scoped press covers only part of a project: without it, a run over one
    #: agent's reports made every later press for that project read as "already
    #: running", and the reports outside that agent were in no agent's prompt.
    agent_scope = models.CharField(max_length=100, default="all")
    #: Where it landed: verdict counts on success, the failure reason otherwise.
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One live run per project, enforced rather than checked. The view
            # read in-flight state and then created rows, so two overlapping
            # presses of "did recent commits fix these?" both saw an empty
            # backlog of runs and both launched a full paid agent per project.
            # Same argument WorkerCommand's partial unique constraints make:
            # the constraint is the check, because a check-then-create is a race.
            # Keyed on the chunk too, because one press of a 120-report backlog
            # is legitimately three launched runs for the one project. Keyed on
            # kind as well: a live "did recent commits fix these" run must not
            # block a "did the known fix land" run for the same project — they
            # answer different questions about the same rows.
            models.UniqueConstraint(
                fields=["project", "kind", "chunk_index"],
                condition=models.Q(status="launched"),
                name="unique_launched_recheck_per_project_chunk",
            ),
        ]

    def __str__(self) -> str:
        return f"SecurityRecheckRun: {self.project} ({self.status})"


class CVEAPIToken(models.Model):
    """A pasted Bearer token for the ASF CVE process service, scoped to a PMC.

    The service (cveprocess.apache.org, PR apache/security-vulnogram#252)
    issues short-lived tokens — hours — from ``/users/token`` after a browser
    login: one ``allocate`` and one ``write`` token per PMC the operator
    belongs to. Hours-lived secrets are why this is a table and not a config
    key: a token you paste every few hours cannot live in a YAML file that
    needs an editor and a restart, and ``.env`` is no better.

    Hygiene rules the rest of the codebase keeps: the value is write-only
    (never rendered after submit, never logged), it is excluded from CSV
    export and sheet sync, and a 401 from the service deletes the row — the
    service gives no expiry metadata, so an auth failure is the only
    trustworthy "this token is dead" signal there is.
    """

    OP_CHOICES = [
        ("allocate", "Allocate CVE ids"),
        ("write", "Read and update CVE records"),
    ]

    #: Lower-case PMC name the token is scoped to ("spark"). Matches
    #: ``ProjectConfig.cve_process_pmc``.
    pmc = models.CharField(max_length=100)
    op = models.CharField(max_length=20, choices=OP_CHOICES)
    token = models.CharField(max_length=200)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pmc", "op"], name="unique_cve_api_token_per_pmc_op"),
        ]
        ordering = ["pmc", "op"]

    def __str__(self) -> str:
        # Deliberately not the token: __str__ lands in admin lists and logs.
        return f"CVEAPIToken: {self.pmc} ({self.op})"


class SecurityVerification(models.Model):
    """One branch's answer to "is this reported vulnerability actually there?".

    Triage rules on the *report* — is it plausible, is it a known CVE, is it
    documented behaviour — from the text. This is the other question, and only
    the code can answer it: a coding agent is put in a checkout of the project
    with the report in hand and told to go and look.

    One row per branch, not one per report, because that is the shape of the
    answer. A deserialization hole in the RPC path may be real on ``master``,
    gone from ``branch-4.0`` where the code was rewritten, and still present in
    ``branch-3.5`` which the project is also shipping. "Is it real?" has no
    single answer for a repo with live release branches, and a verdict with no
    branch attached is not something a maintainer can act on.
    """

    VERDICT_CHOICES = [
        ("affected", "Affected"),
        ("not-affected", "Not affected"),
        ("unclear", "Unclear"),
        ("error", "Could not check"),
    ]

    report = models.ForeignKey(
        SecurityReport,
        on_delete=models.CASCADE,
        related_name="verifications",
    )
    #: Short branch name as git reports it: "master", "branch-3.5".
    branch = models.CharField(max_length=255)
    #: The commit actually examined. Without it a verdict has no shelf life —
    #: "not affected on master" means nothing a month later.
    commit = models.CharField(max_length=64, blank=True, default="")
    verdict = models.CharField(max_length=20, choices=VERDICT_CHOICES, default="unclear")
    #: The agent's own confidence, when it gave one. Kept separate from the
    #: verdict because "affected, 0.4" is a different thing to act on than
    #: "affected, 0.95" and collapsing them loses the distinction.
    confidence = models.FloatField(null=True, blank=True)
    #: The reasoning, as prose. This is what the operator actually reads.
    summary = models.TextField(blank=True, default="")
    #: File/line references the agent based its answer on, one per line. The
    #: difference between a verdict and a verdict somebody can check.
    evidence = models.TextField(blank=True, default="")
    #: Which release *line* this branch ships, and whether it's affected:
    #: ``[{"name": "3.5.x", "status": "affected", "reason": "..."}, ...]``.
    #: ``status`` is one of the :attr:`VERDICT_CHOICES` values minus ``error``.
    #: Normally one entry — a branch ships one line.
    #:
    #: Separate from ``verdict`` because a branch name is not something you can
    #: write in an advisory, and the translation is not mechanical: whether
    #: ``branch-3.5`` means 3.5.x or something else is in the build files, and only
    #: something standing in the checkout can read them. Line granularity on
    #: purpose — everything released on an affected line is assumed affected, which
    #: is not exactly true and is close enough to act on, and it costs the agent a
    #: glance at pom.xml rather than an archaeology dig through tags.
    #:
    #: A list of small dicts rather than its own table: this is the agent's report
    #: of what it found, not a normalised fact about releases, and it is only ever
    #: read back whole. Attacker-influenced (it derives from an agent reading a
    #: report a stranger wrote), so :func:`security.verifier.parse_version_impact`
    #: normalises and caps it before it lands here — nothing downstream should
    #: assume more than "a short list of dicts of short strings".
    version_impact = models.JSONField(default=list, blank=True)
    #: Which agent and model produced this, because the answer is only as good as
    #: its source and that changes between runs.
    agent = models.CharField(max_length=100, blank=True, default="")
    #: Position in the run's branch order, default branch first. Persisted because
    #: the *verifier* already resolves the real default from
    #: ``git symbolic-ref refs/remotes/origin/HEAD`` — "not a guess between master
    #: and main", per its own docstring — and a view that re-guesses
    #: ("main","master","trunk") sorts the default row last for any project on
    #: `develop`, `2.x` or `release`, which is precisely the complaint that
    #: replaced the old bare order_by("branch").
    branch_order = models.IntegerField(default=0)
    #: Raw output, kept when parsing found no structured verdict. The fallback
    #: that stops an unparseable run looking like a clean "unclear".
    raw_output = models.TextField(blank=True, default="")
    duration_seconds = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "branch"]
        constraints = [
            # One verdict per report per branch: a re-run replaces rather than
            # accumulates, so the detail page shows the current answer instead of
            # a pile of them in unclear order.
            models.UniqueConstraint(
                fields=["report", "branch"],
                name="unique_verification_per_report_branch",
            ),
        ]

    def __str__(self) -> str:
        return f"SecurityVerification(report={self.report_id}, {self.branch}): {self.verdict}"


class SecurityTriageFeedback(models.Model):
    """Operator agree/disagree feedback on a single triage verdict.

    Mirrors the anti-pattern learning loop (see :class:`AntiPattern`), but
    for security triage: each feedback row is a snapshot of what the LLM
    said and whether the operator agreed, plus an optional comment. These
    rows are the raw material :func:`distill_triage_guidance` summarizes
    into a :class:`SecurityTriageGuidance` addendum.
    """

    report = models.ForeignKey(
        SecurityReport,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="triage_feedback",
        null=True,
        blank=True,
    )
    agreed = models.BooleanField()
    operator_comment = models.TextField(blank=True, default="")
    triage_summary_snapshot = models.TextField(blank=True, default="")
    assessed_severity_snapshot = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        verdict = "agreed" if self.agreed else "disagreed"
        return f"SecurityTriageFeedback: {verdict} on report {self.report_id}"


class SecurityTriageGuidance(models.Model):
    """Learned triage guidance distilled from operator feedback.

    One active row per project (plus an optional global row with
    ``project=None``), analogous to :class:`AntiPattern`. The
    ``guidance_text`` is injected into future triage system prompts via
    ``security.learning.resolve_triage_guidance``.
    """

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="triage_guidance",
        null=True,
        blank=True,
    )
    guidance_text = models.TextField(blank=True, default="")
    source_feedback_count = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"SecurityTriageGuidance(project={self.project_id}): {self.guidance_text[:60]}"


class EmailScanRecord(models.Model):
    """Audit record of a single email the security inbox poller examined.

    Exists purely for transparency: it lets the operator see *exactly* which
    of their emails the system read and why it did or didn't turn one into a
    SecurityReport. The inbox poller is read-only (it never marks messages
    seen and never sends anything), and one of these rows is written for
    every message it opens.
    """

    ACTION_CHOICES = [
        ("ingested", "Ingested as security report"),
        ("skipped_not_security", "Skipped — did not match security filter"),
        ("skipped_duplicate", "Skipped — already ingested"),
    ]

    scanned_at = models.DateTimeField(default=timezone.now)
    folder = models.CharField(max_length=255, blank=True, default="")
    # IMAP Message-ID header — used to dedup across polls without mutating
    # the mailbox (we never set the \Seen flag).
    message_id = models.CharField(max_length=500, blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    from_name = models.CharField(max_length=255, blank=True, default="")
    from_email = models.CharField(max_length=255, blank=True, default="")
    is_forwarded = models.BooleanField(default=False)
    # Which security keywords matched, so the filter decision is inspectable.
    matched_keywords = models.JSONField(default=list, blank=True)
    classified_security = models.BooleanField(default=False)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, default="skipped_not_security")
    security_report = models.ForeignKey(
        SecurityReport,
        on_delete=models.SET_NULL,
        related_name="scan_records",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-scanned_at"]
        indexes = [models.Index(fields=["-scanned_at"])]

    def __str__(self) -> str:
        return f"EmailScanRecord({self.action}): {self.subject[:60]}"


class Alert(models.Model):
    """An alert-mode notification raised by the worker.

    Two kinds today: someone else's PR overlaps work the operator has in
    flight ("working-overlap"), and a security report is sitting in the
    queue or in triage ("security-report"). Rows double as the dedup
    ledger — ``dedup_key`` is unique, so each PR/report alerts at most
    once — and as the audit log of what frank emailed (or would have
    emailed, when no recipient is configured).
    """

    ALERT_TYPE_CHOICES = [
        ("working-overlap", "Working Overlap"),
        ("security-report", "Security Report"),
    ]

    alert_type = models.CharField(max_length=30, choices=ALERT_TYPE_CHOICES)
    # One row per alerted entity, e.g. "working-overlap:pr:42" or
    # "security-report:report:7". A plain unique column (rather than
    # partial unique constraints over the nullable FKs) keeps SQLite happy.
    dedup_key = models.CharField(max_length=100, unique=True)

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="alerts", null=True, blank=True
    )
    pull_request = models.ForeignKey(
        PullRequest, on_delete=models.CASCADE, related_name="alerts", null=True, blank=True
    )
    security_report = models.ForeignKey(
        SecurityReport, on_delete=models.CASCADE, related_name="alerts", null=True, blank=True
    )

    title = models.CharField(max_length=500)
    # Human-readable reason strings, e.g. which files overlap which of the
    # operator's open PRs, or which working_paths pattern matched.
    reasons = models.JSONField(default=list, blank=True)

    email_sent = models.BooleanField(default=False)
    emailed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    # Explicit manager declaration so django-stubs can resolve
    # ``Alert.objects`` (same workaround as WorkerCommand below).
    objects: models.Manager[Alert] = models.Manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email_sent", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Alert[{self.alert_type}]: {self.title}"


class WorkerCommand(models.Model):
    """A queued action requested from the dashboard for the worker to run.

    Lets the web container hand off Docker / long-running operations
    (differential test runs, security sandbox, force-run agents) to the
    worker container instead of spawning subprocesses or threads from
    inside the request path. The web view inserts a row with
    ``status="pending"``; the worker picks it up on its next cycle.
    """

    COMMAND_CHOICES = [
        ("run_dual_tests", "Run differential tests"),
        ("run_security_sandbox", "Run security report sandbox"),
        ("poll_security_rechecks", "Wait on batch security rechecks"),
        ("run_security_triage", "Run LLM triage on security report"),
        ("run_agents", "Force-run review agents"),
        ("check_fix_landed", "Check whether one report's fix landed upstream"),
        ("check_fixes_landed", "Sweep: check whether known fixes landed upstream"),
        ("scan_zombie_prs", "Sweep: closed-unmerged PRs whose JIRA is still open"),
        ("assess_zombie_pr", "Re-run the zombie assessment on one PR"),
    ]

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    command = models.CharField(max_length=50, choices=COMMAND_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    # Either a PR or a SecurityReport target. At least one must be set.
    pull_request = models.ForeignKey(
        PullRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="worker_commands",
    )
    security_report = models.ForeignKey(
        SecurityReport,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="worker_commands",
    )

    # Higher runs first, then oldest-first within a priority. Zero is bulk work
    # nobody is watching; the dashboard's buttons queue above it.
    #
    # This exists because the queue was strictly FIFO, and one bulk import with
    # --triage puts a thousand run_security_triage rows in front of the operator.
    # Each is an NVD lookup plus two LLM calls, so "Force Run Agents" — a click,
    # with someone sitting there waiting for it — landed hours later while the
    # dashboard cheerfully said "in a few minutes".
    priority = models.SmallIntegerField(default=0)

    # Optional command-specific arguments (e.g. force flags).
    kwargs = models.JSONField(default=dict)
    log = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    # Explicit manager declaration so django-stubs can resolve
    # ``WorkerCommand.objects`` in type-checking. Without this declaration
    # the plugin sometimes fails to attach the default manager to newly
    # introduced models.
    objects: models.Manager[WorkerCommand] = models.Manager()

    class Meta:
        ordering = ["-priority", "created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            # The claim query's exact shape: pending rows, best first.
            models.Index(fields=["status", "-priority", "created_at"]),
        ]
        constraints = [
            # At most one queued/running command of a kind per security report.
            # Two doors queue triage (auto on ingest, the operator's button),
            # and each run costs an NVD lookup plus two LLM calls, so a
            # check-then-create race is worth a real constraint rather than
            # hoping the two requests don't interleave. NULL security_report
            # (the PR-targeted commands) never conflicts.
            models.UniqueConstraint(
                fields=["command", "security_report"],
                condition=models.Q(status__in=["pending", "running"]),
                name="unique_inflight_command_per_report",
            ),
            # The same rule for the PR-targeted commands, which had none: SQL
            # treats NULLs as distinct, so the constraint above covered exactly
            # the rows whose security_report is set. "Force Run Agents" is a
            # 30-120s pipeline behind an htmx button that says "reload in a few
            # minutes", so an operator who reloads and clicks again queued a
            # second full run — five impatient clicks, five sequential runs, and
            # the button looking broken the whole time.
            models.UniqueConstraint(
                fields=["command", "pull_request"],
                condition=models.Q(status__in=["pending", "running"]),
                name="unique_inflight_command_per_pr",
            ),
        ]

    def __str__(self) -> str:
        if self.pull_request is not None:
            target = f"PR#{self.pull_request.pk}"
        elif self.security_report is not None:
            target = f"Report#{self.security_report.pk}"
        else:
            target = "<no target>"
        return f"{self.command} on {target} [{self.status}]"


class ZombieAssessment(models.Model):
    """What the agent made of one zombie: a closed-unmerged PR for an open JIRA.

    One row per PR, created by the zombie scan the moment a PR qualifies —
    closed without merging, names a JIRA ticket, and that ticket is not
    closed. Created *before* the model is asked anything, so the Zombie Queue
    lists what was found even when no LLM backend is configured or the budget
    for this run is spent; ``verdict="unassessed"`` is that state, and the next
    run picks those up newest-first.

    Two questions, answered separately on purpose: a JIRA can still be worth
    doing while the PR that tried was the wrong approach, and a sound fix can
    be for a ticket the project has since decided against. ``needed_changes``
    is the "little TLC" list — what would have to change to land this today.

    ``disposition`` is the operator's, never the machine's: dismissing hides a
    row from the queue without deleting the evidence, so a re-scan does not
    resurrect it (the scan skips any PR that already has a row).
    """

    VERDICT_CHOICES = [
        ("unassessed", "Not yet assessed"),
        ("revive", "Revive as-is"),
        ("revive-with-changes", "Revive with changes"),
        ("rethink", "JIRA valid, fix needs a new approach"),
        ("drop", "Not worth reviving"),
        ("unclear", "Unclear"),
    ]

    DISPOSITION_CHOICES = [
        ("pending", "Pending"),
        ("dismissed", "Dismissed"),
    ]

    #: Attempts after which the backlog pass stops retrying an unparseable one.
    MAX_ATTEMPTS = 3

    pull_request = models.OneToOneField(
        PullRequest, on_delete=models.CASCADE, related_name="zombie_assessment"
    )
    jira_ticket_id = models.CharField(max_length=50)
    jira_status = models.CharField(max_length=100, blank=True, default="")
    jira_summary = models.CharField(max_length=1000, blank=True, default="")
    # Stored rather than re-derived from the project config, so the list page
    # can link a ticket without loading YAML per row.
    jira_url = models.URLField(max_length=1000, blank=True, default="")
    pr_closed_at = models.DateTimeField(null=True, blank=True)
    # The last few conversation comments, formatted — usually the reason the PR
    # was closed (a stale bot, "superseded by #N", a reviewer's objection).
    closing_context = models.TextField(blank=True, default="")

    verdict = models.CharField(max_length=30, choices=VERDICT_CHOICES, default="unassessed")
    jira_makes_sense = models.BooleanField(null=True, blank=True)
    jira_reasoning = models.TextField(blank=True, default="")
    fix_makes_sense = models.BooleanField(null=True, blank=True)
    fix_reasoning = models.TextField(blank=True, default="")
    needed_changes = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True, default="")
    confidence = models.FloatField(null=True, blank=True)
    backend_used = models.CharField(max_length=100, blank=True, default="")
    attempts = models.IntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default="")
    assessed_at = models.DateTimeField(null=True, blank=True)

    disposition = models.CharField(
        max_length=20, choices=DISPOSITION_CHOICES, default="pending", db_index=True
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects: models.Manager[ZombieAssessment] = models.Manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Zombie {self.jira_ticket_id} / {self.pull_request} [{self.verdict}]"

    @property
    def is_assessed(self) -> bool:
        return self.verdict != "unassessed"

    @property
    def gave_up(self) -> bool:
        """Whether the backlog pass has stopped retrying this one."""
        return not self.is_assessed and self.attempts >= self.MAX_ATTEMPTS


class ZombieScanState(models.Model):
    """Where the zombie scan got to in one project's closed-PR history.

    The scan works backwards — newest closed PR first — through a list that on
    Spark is tens of thousands long, so one run cannot do it and the next run
    has to pick up where this one stopped. GitHub lists closed PRs by creation
    date descending, and PR numbers increase with creation, so the lowest
    number examined so far is a strict frontier: everything above it has been
    looked at. ``next_page`` is a hint for where that frontier now sits; the
    list shifts as PRs close, so a page may carry a few already-examined numbers
    at its top, and the frontier is what makes skipping them safe.
    """

    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="zombie_scan")
    lowest_number_seen = models.IntegerField(null=True, blank=True)
    next_page = models.IntegerField(default=1)
    # Reached the end of the closed-PR list. A later run starts over from page 1
    # with the frontier intact, so it only examines PRs closed since.
    exhausted = models.BooleanField(default=False)
    # PR numbers whose JIRA fetch failed, to re-examine next run. The frontier
    # has already moved past them, so without this a JIRA outage mid-walk
    # dropped every zombie in that stretch for good.
    jira_retry = models.JSONField(default=list, blank=True)
    prs_examined = models.IntegerField(default=0)
    candidates_found = models.IntegerField(default=0)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_summary = models.TextField(blank=True, default="")

    objects: models.Manager[ZombieScanState] = models.Manager()

    def __str__(self) -> str:
        return f"ZombieScanState[{self.project}] frontier=#{self.lowest_number_seen}"
