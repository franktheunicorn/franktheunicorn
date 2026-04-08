"""Resolve effective config values from operator.yaml.

This module is the bridge between ``operator.yaml`` and Django
``settings.py``.  It loads the YAML config (with ``${VAR}`` expansion
already applied by the loader), fills in defaults for empty fields,
and returns a flat dict that ``settings.py`` can assign directly.

**No ``django.conf.settings`` import** — this module receives
``base_dir`` as a parameter so it can run before Django settings are
fully configured, avoiding circular imports.
"""

from __future__ import annotations

import os
from pathlib import Path

from franktheunicorn.config.loader import load_operator_config
from franktheunicorn.config.models import OperatorConfig


def resolve_operator_config_path(base_dir: Path) -> str:
    """Determine the operator.yaml path.

    Precedence: ``FRANK_OPERATOR_CONFIG`` env var → ``config/active/operator.yaml``
    → ``config/examples/operator.yaml``.
    """
    env_override = os.environ.get("FRANK_OPERATOR_CONFIG")
    if env_override:
        return env_override
    active = base_dir / "config" / "active" / "operator.yaml"
    if active.exists():
        return str(active)
    return str(base_dir / "config" / "examples" / "operator.yaml")


def _resolve_projects_dir(base_dir: Path, oc: OperatorConfig) -> str:
    """Determine the projects directory, applying the same fallback as before."""
    env_override = os.environ.get("FRANK_PROJECTS_DIR")
    if env_override:
        return env_override
    if oc.projects_dir:
        return oc.projects_dir
    active_projects = base_dir / "config" / "active" / "projects"
    if any(active_projects.glob("*.yaml")):
        return str(active_projects)
    return str(base_dir / "config" / "examples" / "projects")


def resolve_config(base_dir: Path) -> tuple[OperatorConfig, dict[str, str | int | bool]]:
    """Load operator config and resolve all settings.

    Returns ``(operator_config, resolved_settings_dict)``.
    The resolved dict uses YAML values with sensible defaults for
    empty strings.  ``${…}`` expansion has already happened at YAML
    load time.
    """
    config_path = resolve_operator_config_path(base_dir)
    oc = load_operator_config(config_path)

    # Infer GitHub username from token if not explicitly set.
    if not oc.github_username and oc.github_token and not oc.mock_mode:
        from franktheunicorn.github.client import infer_github_username

        inferred = infer_github_username(oc.github_token)
        if inferred:
            oc.github_username = inferred

    resolved: dict[str, str | int | bool] = {
        "config_path": config_path,
        "mock_mode": oc.mock_mode,
        "data_dir": oc.data_dir or str(base_dir / "data"),
        "github_token": oc.github_token,
        "fixtures_dir": oc.fixtures_dir or str(base_dir / "config" / "fixtures"),
        "repos_dir": oc.repos_dir or str(base_dir / "data" / "repos"),
        "projects_dir": _resolve_projects_dir(base_dir, oc),
        "poll_interval": oc.poll_interval_seconds or 300,
        "digest_email": oc.digest_email,
        # Email
        "email_host": oc.email.smtp_host,
        "email_port": oc.email.smtp_port,
        "email_host_user": oc.email.smtp_user,
        "email_host_password": oc.email.smtp_pass,
        "email_from": oc.email.from_address,
        "email_use_tls": oc.email.use_tls,
    }

    return oc, resolved
