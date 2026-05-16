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


def workspace_context(request: HttpRequest) -> dict[str, Any]:
    workspace = request.COOKIES.get("workspace", "all")
    workspaces: list[dict[str, str]] = [{"key": "all", "label": "All Projects"}]
    try:
        from django.conf import settings

        from franktheunicorn.config.loader import load_operator_config

        config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
        raw = getattr(config, "workspaces", {})
        if raw and isinstance(raw, dict):
            for key, val in raw.items():
                desc = str(val.get("description", key)) if isinstance(val, dict) else str(key)
                workspaces.append({"key": key, "label": desc})
    except Exception:
        pass
    return {"workspace": workspace, "workspaces": workspaces}
