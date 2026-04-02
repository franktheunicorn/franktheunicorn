"""Tests for Django admin registration and configuration."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib import admin
from django.contrib.admin.sites import AdminSite

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
from tests.factories import AntiPatternFactory

ALL_MODELS = [Project, PullRequest, ReviewDraft, AntiPattern, OperatorAction]


@pytest.mark.parametrize("model_class", ALL_MODELS, ids=lambda m: m.__name__)
class TestAdminRegistration:
    """Verify all models are registered with the admin site."""

    def test_model_registered(self, model_class: type) -> None:
        assert model_class in admin.site._registry


ADMIN_CONFIG = [
    (
        ProjectAdmin,
        Project,
        {"list_display": ("owner", "repo", "enabled"), "search_fields": ("owner", "repo")},
    ),
    (
        PullRequestAdmin,
        PullRequest,
        {
            "list_display": ("number", "title", "interest_score"),
            "list_filter": ("state", "is_draft"),
        },
    ),
    (
        ReviewDraftAdmin,
        ReviewDraft,
        {"list_display": ("pull_request", "file_path", "status"), "list_filter": ("status",)},
    ),
    (
        OperatorActionAdmin,
        OperatorAction,
        {"list_display": ("action_type", "pull_request"), "list_filter": ("action_type",)},
    ),
]


@pytest.mark.parametrize(
    ("admin_cls", "model_cls", "expected"),
    ADMIN_CONFIG,
    ids=lambda x: x.__name__ if isinstance(x, type) else "",
)
class TestAdminConfig:
    """Verify admin classes have expected configuration."""

    def test_list_display(self, admin_cls: type, model_cls: type, expected: dict[str, Any]) -> None:
        instance = admin_cls(model_cls, AdminSite())
        for field in expected.get("list_display", ()):
            assert field in instance.list_display

    def test_list_filter(self, admin_cls: type, model_cls: type, expected: dict[str, Any]) -> None:
        instance = admin_cls(model_cls, AdminSite())
        for field in expected.get("list_filter", ()):
            assert field in instance.list_filter

    def test_search_fields(
        self, admin_cls: type, model_cls: type, expected: dict[str, Any]
    ) -> None:
        instance = admin_cls(model_cls, AdminSite())
        for field in expected.get("search_fields", ()):
            assert field in instance.search_fields


class TestAntiPatternAdminDisplay:
    """Test custom display methods on AntiPatternAdmin."""

    @pytest.mark.django_db
    def test_short_text_not_truncated(self) -> None:
        ap_admin = AntiPatternAdmin(AntiPattern, AdminSite())
        ap = AntiPatternFactory(pattern_text="short text")
        assert ap_admin.pattern_text_short(ap) == "short text"

    @pytest.mark.django_db
    def test_long_text_truncated(self) -> None:
        ap_admin = AntiPatternAdmin(AntiPattern, AdminSite())
        long_ap = AntiPatternFactory(pattern_text="x" * 100)
        result = ap_admin.pattern_text_short(long_ap)
        assert result.endswith("...")
        assert len(result) == 63
