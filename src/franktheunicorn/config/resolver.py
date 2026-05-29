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
from franktheunicorn.config.models import ForgeRegistryEntry, OperatorConfig


def get_forge_entry(oc: OperatorConfig, name: str) -> ForgeRegistryEntry:
    """Look up a forge by name in the operator's registry.

    Raises ``KeyError`` if the name is not registered, listing the
    available names — projects misconfigured against a missing forge
    should fail loudly at config-resolution time, not silently.
    """
    for entry in oc.forges:
        if entry.name == name:
            return entry
    available = ", ".join(e.name for e in oc.forges) or "(empty registry)"
    msg = f"forge {name!r} not found in operator registry; available: {available}"
    raise KeyError(msg)


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


_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


def _resolve_log_level(oc: OperatorConfig) -> str:
    """Resolve effective log level. ``FRANK_LOG_LEVEL`` env var wins over YAML."""
    env_override = os.environ.get("FRANK_LOG_LEVEL", "").strip().upper()
    if env_override in _VALID_LOG_LEVELS:
        return env_override
    return oc.log_level or "INFO"


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

    Note: This does NOT make any network calls. Username inference from a
    token requires a separate explicit call to ``ensure_github_username``,
    which the worker performs after settings load. Keeping settings load
    purely local-first means ``manage.py check`` / ``migrate`` / test
    runs do not depend on GitHub being reachable.
    """
    config_path = resolve_operator_config_path(base_dir)
    oc = load_operator_config(config_path)

    resolved: dict[str, str | int | bool] = {
        "config_path": config_path,
        "mock_mode": oc.mock_mode,
        "data_dir": oc.data_dir or str(base_dir / "data"),
        "github_token": oc.github_token,
        "fixtures_dir": oc.fixtures_dir or str(base_dir / "config" / "fixtures"),
        "repos_dir": oc.repos_dir or str(base_dir / "data" / "repos"),
        "projects_dir": _resolve_projects_dir(base_dir, oc),
        "poll_interval": oc.poll_interval_seconds or 300,
        "log_level": _resolve_log_level(oc),
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


def ensure_github_username(oc: OperatorConfig) -> None:
    """Populate ``oc.github_username`` from the token via a GitHub API call,
    if a token is set and a username is not. No-op in mock mode.

    Mutates ``oc`` in place. This is called by the worker after settings
    load so Django settings.py never depends on a live GitHub connection.
    """
    if oc.github_username or not oc.github_token or oc.mock_mode:
        return

    from franktheunicorn.backends.github import infer_github_username

    inferred = infer_github_username(oc.github_token)
    if inferred:
        oc.github_username = inferred
