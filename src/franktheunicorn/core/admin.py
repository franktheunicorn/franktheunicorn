"""Django admin configuration for franktheunicorn core models."""

from django.contrib import admin

from franktheunicorn.core.models import (
    AgentFeedback,
    Alert,
    AntiPattern,
    CostRecord,
    EmailScanRecord,
    LLMBackendFallback,
    OperatorAction,
    Project,
    PullRequest,
    ReviewDraft,
    SecurityReport,
    TestRun,
)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for monitored GitHub projects."""

    list_display = ("owner", "repo", "project_type", "enabled", "review_context", "created_at")
    list_filter = ("enabled",)
    search_fields = ("owner", "repo")
    readonly_fields = ("created_at", "updated_at")


@admin.register(PullRequest)
class PullRequestAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for ingested pull requests."""

    list_display = (
        "number",
        "title",
        "project",
        "author",
        "state",
        "interest_score",
        "is_draft",
        "github_updated_at",
    )
    list_filter = (
        "state",
        "is_draft",
        "likely_ai_generated",
        "ai_agent_source",
        "queue",
        "project",
    )
    search_fields = ("title", "author")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ReviewDraft)
class ReviewDraftAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for LLM-generated review drafts."""

    list_display = (
        "pull_request",
        "file_path",
        "sources",
        "status",
        "severity",
        "confidence",
        "created_at",
    )
    list_filter = ("status", "severity", "category", "tone_guard_applied")
    search_fields = ("file_path", "comment_body")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AntiPattern)
class AntiPatternAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for anti-pattern feedback entries."""

    list_display = (
        "pattern_text_short",
        "project",
        "weight",
        "times_triggered",
        "is_active",
        "last_matched_at",
        "created_at",
    )
    list_filter = ("project", "is_active")
    search_fields = ("pattern_text", "description")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Pattern")
    def pattern_text_short(self, obj: AntiPattern) -> str:
        return obj.pattern_text[:60] + ("..." if len(obj.pattern_text) > 60 else "")


@admin.register(OperatorAction)
class OperatorActionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for operator action audit log."""

    list_display = ("action_type", "pull_request", "review_draft", "created_at")
    list_filter = ("action_type",)
    search_fields = ("notes",)
    readonly_fields = ("created_at",)


@admin.register(CostRecord)
class CostRecordAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for LLM cost tracking."""

    list_display = (
        "action_type",
        "backend",
        "project",
        "tokens_in",
        "tokens_out",
        "estimated_cost_usd",
        "created_at",
    )
    list_filter = ("action_type", "backend", "project")
    readonly_fields = ("created_at",)


@admin.register(TestRun)
class TestRunAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for differential test runs."""

    list_display = (
        "pull_request",
        "run_type",
        "status",
        "differential_verdict",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "differential_verdict", "run_type")
    readonly_fields = ("created_at",)


@admin.register(AgentFeedback)
class AgentFeedbackAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for agent feedback records (v1.25)."""

    list_display = (
        "pull_request",
        "assessment",
        "feedback_method",
        "sent_at",
        "created_at",
    )
    list_filter = ("assessment", "feedback_method")
    readonly_fields = ("created_at",)


@admin.register(LLMBackendFallback)
class LLMBackendFallbackAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for persisted LLM backend compatibility-probe state."""

    list_display = (
        "provider",
        "model",
        "base_url",
        "token_param",
        "supports_json_object",
        "updated_at",
    )
    list_filter = ("provider", "supports_json_object")
    search_fields = ("provider", "model", "base_url")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SecurityReport)
class SecurityReportAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for triaged security reports."""

    list_display = ("title", "status", "assessed_severity", "source", "created_at")
    list_filter = ("status", "assessed_severity", "source")
    search_fields = ("title", "reporter_name", "reporter_email", "parsed_component")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for alert-mode notifications."""

    list_display = (
        "alert_type",
        "title",
        "project",
        "pull_request",
        "security_report",
        "email_sent",
        "created_at",
    )
    list_filter = ("alert_type", "email_sent", "project")
    search_fields = ("title", "dedup_key")
    readonly_fields = ("created_at", "emailed_at")


@admin.register(EmailScanRecord)
class EmailScanRecordAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Read-only audit of every email the security scanner examined."""

    list_display = (
        "scanned_at",
        "from_email",
        "subject",
        "classified_security",
        "is_forwarded",
        "action",
    )
    list_filter = ("action", "classified_security", "is_forwarded")
    search_fields = ("subject", "from_email", "from_name", "message_id")
    readonly_fields = (
        "scanned_at",
        "folder",
        "message_id",
        "subject",
        "from_name",
        "from_email",
        "is_forwarded",
        "matched_keywords",
        "classified_security",
        "action",
        "security_report",
    )
