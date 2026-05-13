"""Dashboard-wide template context processors."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from franktheunicorn.core.models import Project


def nav_projects(request: HttpRequest) -> dict[str, Any]:
    return {
        "nav_projects": list(
            Project.objects.filter(enabled=True).order_by("owner", "repo").values("owner", "repo")
        )
    }
