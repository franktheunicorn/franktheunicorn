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


class PullRequest(models.Model):
    """A pull request ingested from GitHub."""

    STATE_CHOICES = [
        ("open", "Open"),
        ("closed", "Closed"),
        ("merged", "Merged"),
    ]

    QUEUE_CHOICES = [
        ("review", "Review"),
        ("ai-generated", "AI-Generated"),
        ("new-contributor", "New Contributor"),
        ("consider-closing", "Consider Closing"),
        ("needs-triage", "Needs Triage"),
        ("your-prs", "Your PRs"),
        ("mentioned", "Mentioned"),
        ("wip", "WIP"),
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
    score_breakdown = models.JSONField(default=dict, blank=True)

    # Flags
    is_draft = models.BooleanField(default=False)
    likely_ai_generated = models.BooleanField(default=False)
    is_operator_pr = models.BooleanField(default=False)
    is_new_contributor = models.BooleanField(default=False)
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

    # Cached context (v1.5)
    jira_ticket_id = models.CharField(max_length=50, blank=True, default="")
    jira_cache = models.JSONField(null=True, blank=True)
    community_context_cache = models.JSONField(null=True, blank=True)
    sentry_context_cache = models.JSONField(null=True, blank=True)

    # Queue routing (§2.2)
    queue = models.CharField(max_length=50, choices=QUEUE_CHOICES, default="review")

    # Shepherding (v2 — §2.3)
    last_shepherded_at = models.DateTimeField(null=True, blank=True)
    reviewer_comment_count = models.IntegerField(default=0)

    # Merge queue (v2)
    ci_status = models.CharField(max_length=20, blank=True, default="")
    approval_count = models.IntegerField(default=0)
    merge_queue_eligible = models.BooleanField(default=False)
    merged_at = models.DateTimeField(null=True, blank=True)
    merged_by = models.CharField(max_length=255, blank=True, default="")

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
    # ``{"at": iso8601, "status": "ok"|"failed"|"no-checkout", "findings": int}``.
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
        ordering = ["-interest_score", "-github_updated_at"]

    def __str__(self) -> str:
        return f"#{self.number} {self.title}"


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
    ]

    SOURCE_CHOICES = [
        ("paste", "Pasted"),
        ("email", "Email"),
        ("zip", "Zip import"),
    ]

    SEVERITY_CHOICES = [
        ("critical", "Critical"),
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
        ("informational", "Informational"),
        ("unknown", "Unknown"),
    ]

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

    # LLM-parsed structured fields (populated by triage pipeline)
    parsed_component = models.CharField(max_length=500, blank=True, default="")
    parsed_poc = models.TextField(blank=True, default="")
    parsed_impact = models.TextField(blank=True, default="")
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

    # CVE dedup — against the *public* record.
    cve_matches = models.JSONField(default=list, blank=True)
    matched_cve_id = models.CharField(max_length=50, blank=True, default="")

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

    # Email metadata (populated when source=email)
    email_message_id = models.CharField(max_length=500, blank=True, default="")
    email_received_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"SecurityReport: {self.title or self.raw_text[:60]}"


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
        ("run_security_triage", "Run LLM triage on security report"),
        ("run_agents", "Force-run review agents"),
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
