"""Django admin configuration for franktheunicorn core models."""

from django.contrib import admin

from franktheunicorn.core.models import (
    AntiPattern,
    OperatorAction,
    Project,
    PullRequest,
    ReviewDraft,
)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("owner", "repo", "enabled", "review_context", "created_at")
    list_filter = ("enabled",)
    search_fields = ("owner", "repo")


@admin.register(PullRequest)
class PullRequestAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
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
    list_filter = ("state", "is_draft", "likely_ai_generated", "project")
    search_fields = ("title", "author")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ReviewDraft)
class ReviewDraftAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("pull_request", "file_path", "status", "confidence", "created_at")
    list_filter = ("status",)
    search_fields = ("file_path", "comment_body")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AntiPattern)
class AntiPatternAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("pattern_text_short", "project", "weight", "times_triggered", "created_at")
    list_filter = ("project",)
    search_fields = ("pattern_text", "description")

    @admin.display(description="Pattern")
    def pattern_text_short(self, obj: AntiPattern) -> str:
        return obj.pattern_text[:60] + ("..." if len(obj.pattern_text) > 60 else "")


@admin.register(OperatorAction)
class OperatorActionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("action_type", "pull_request", "review_draft", "created_at")
    list_filter = ("action_type",)
    search_fields = ("notes",)
    readonly_fields = ("created_at",)
