"""Configuration loading for operator and per-project settings.

Config lives in local YAML files - no SaaS, no remote config service.
The operator edits these files directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Operator config
# ---------------------------------------------------------------------------


class OperatorConfig(BaseModel):
    """Top-level operator identity and preferences."""

    github_login: str
    email: str | None = None
    # Logins considered trusted collaborators across all projects.
    trusted_collaborators: list[str] = Field(default_factory=list)
    # How many days until a PR is considered stale.
    stale_pr_days: int = 30


# ---------------------------------------------------------------------------
# Per-project config
# ---------------------------------------------------------------------------


class ProjectConfig(BaseModel):
    """Configuration for a single monitored repository."""

    # Unique slug used as primary key in the projects table.
    slug: str
    # GitHub owner/repo, e.g. "apache/spark".
    repo: str
    # Human-readable context description fed into the review prompt.
    review_context: str = ""
    # Filesystem paths that, when touched by a PR, boost interest score.
    watched_paths: list[str] = Field(default_factory=list)
    # Logins of frequent contributors (gets a scoring bump).
    frequent_contributors: list[str] = Field(default_factory=list)
    # Whether ASF/Apache governance norms apply.
    asf_project: bool = False
    # Poll interval in seconds (per project override).
    poll_interval_seconds: int = 300
    # Maximum number of open PRs to ingest per poll.
    max_prs_per_poll: int = 50
    # Whether this project is enabled.
    enabled: bool = True


# ---------------------------------------------------------------------------
# App settings (env-driven)
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Environment-driven application settings.

    Sensitive values (tokens) come from env vars.
    Structural config (repos, operator) comes from YAML files.
    """

    model_config = SettingsConfigDict(env_prefix="FRANK_", env_file=".env", extra="ignore")

    github_token: str = ""
    database_url: str = "sqlite:///./data/franktheunicorn.db"
    # Path to operator YAML file.
    operator_config_path: str = "configs/operator.yaml"
    # Directory containing per-project YAML files.
    projects_config_dir: str = "configs/projects"
    # Log level.
    log_level: str = "INFO"
    # Worker poll interval fallback (seconds).
    default_poll_interval_seconds: int = 300
    # Dashboard host/port.
    web_host: str = "0.0.0.0"
    web_port: int = 8000


def load_operator_config(path: str | Path | None = None) -> OperatorConfig:
    """Load operator config from a YAML file."""
    settings = get_settings()
    config_path = Path(path or settings.operator_config_path)
    if not config_path.exists():
        # Return a minimal default so the system can start without config.
        return OperatorConfig(github_login="unknown")
    with config_path.open() as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}
    return OperatorConfig(**data)


def load_project_configs(directory: str | Path | None = None) -> list[ProjectConfig]:
    """Load all project configs from YAML files in the given directory."""
    settings = get_settings()
    config_dir = Path(directory or settings.projects_config_dir)
    if not config_dir.exists():
        return []
    configs: list[ProjectConfig] = []
    for yaml_file in sorted(config_dir.glob("*.yaml")):
        with yaml_file.open() as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        configs.append(ProjectConfig(**data))
    return configs


_settings_instance: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance


def override_settings(new_settings: Settings) -> None:
    """Replace the cached settings instance (useful in tests)."""
    global _settings_instance
    _settings_instance = new_settings
