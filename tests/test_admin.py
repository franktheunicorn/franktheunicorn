"""Tests for Django admin registration and configuration."""

from __future__ import annotations

import pytest
from django.contrib.admin.sites import AdminSite
from tests.factories import AntiPatternFactory

from franktheunicorn.core.admin import (
    AntiPatternAdmin,
    OperatorActionAdmin,
    ProjectAdmin,
    PullRequestAdmin,
    ReviewDraftAdmin,
)
from franktheunicorn.core.models import (
    AntiPattern,
    OperatorAction,
    Project,
    PullRequest,
    ReviewDraft,
)


class TestAdminRegistration:
    """Verify all models are registered with the admin site."""

    def test_project_registered(self) -> None:
        from django.contrib import admin

        assert Project in admin.site._registry

    def test_pull_request_registered(self) -> None:
        from django.contrib import admin

        assert PullRequest in admin.site._registry

    def test_review_draft_registered(self) -> None:
        from django.contrib import admin

        assert ReviewDraft in admin.site._registry

    def test_anti_pattern_registered(self) -> None:
        from django.contrib import admin

        assert AntiPattern in admin.site._registry

    def test_operator_action_registered(self) -> None:
        from django.contrib import admin

        assert OperatorAction in admin.site._registry


class TestAdminConfig:
    """Verify admin classes have expected configuration."""

    def test_project_admin_list_display(self) -> None:
        admin = ProjectAdmin(Project, AdminSite())
        assert "owner" in admin.list_display
        assert "repo" in admin.list_display
        assert "enabled" in admin.list_display

    def test_project_admin_search_fields(self) -> None:
        admin = ProjectAdmin(Project, AdminSite())
        assert "owner" in admin.search_fields
        assert "repo" in admin.search_fields

    def test_pull_request_admin_list_display(self) -> None:
        admin = PullRequestAdmin(PullRequest, AdminSite())
        assert "number" in admin.list_display
        assert "title" in admin.list_display
        assert "interest_score" in admin.list_display

    def test_pull_request_admin_list_filter(self) -> None:
        admin = PullRequestAdmin(PullRequest, AdminSite())
        assert "state" in admin.list_filter
        assert "is_draft" in admin.list_filter

    def test_review_draft_admin_list_filter(self) -> None:
        admin = ReviewDraftAdmin(ReviewDraft, AdminSite())
        assert "status" in admin.list_filter

    def test_operator_action_admin_list_filter(self) -> None:
        admin = OperatorActionAdmin(OperatorAction, AdminSite())
        assert "action_type" in admin.list_filter

    @pytest.mark.django_db
    def test_anti_pattern_short_text(self) -> None:
        admin = AntiPatternAdmin(AntiPattern, AdminSite())
        short = AntiPatternFactory(pattern_text="short text")
        assert admin.pattern_text_short(short) == "short text"
        long_text = "x" * 100
        long_ap = AntiPatternFactory(pattern_text=long_text)
        result = admin.pattern_text_short(long_ap)
        assert result.endswith("...")
        assert len(result) == 63  # 60 chars + "..."
