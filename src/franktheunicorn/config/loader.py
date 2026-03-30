"""Load operator and project configs from YAML files on disk."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from franktheunicorn.config.models import OperatorConfig, ProjectConfig

logger = logging.getLogger(__name__)


def load_operator_config(path: str | Path) -> OperatorConfig:
    """Load operator config from a YAML file. Returns defaults if file doesn't exist."""
    p = Path(path)
    if not p.exists():
        logger.debug("Operator config not found at %s, using defaults", p)
        return OperatorConfig()
    try:
        with p.open() as f:
            data = yaml.safe_load(f) or {}
        return OperatorConfig(**data)
    except yaml.YAMLError:
        logger.exception("Invalid YAML in operator config: %s", p)
        return OperatorConfig()
    except ValidationError:
        logger.exception("Validation error in operator config: %s", p)
        return OperatorConfig()


def load_project_configs(directory: str | Path) -> list[ProjectConfig]:
    """Load all project configs from YAML files in a directory."""
    d = Path(directory)
    if not d.exists():
        return []
    configs: list[ProjectConfig] = []
    yaml_files = sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml"))
    for yaml_file in yaml_files:
        try:
            with yaml_file.open() as f:
                data = yaml.safe_load(f) or {}
            configs.append(ProjectConfig(**data))
        except yaml.YAMLError:
            logger.exception("Invalid YAML in project config: %s", yaml_file)
        except ValidationError:
            logger.exception("Validation error in project config: %s", yaml_file)
    return configs


def get_operator_config() -> OperatorConfig:
    """Load operator config from the path configured in Django settings."""
    from django.conf import settings

    return load_operator_config(settings.FRANK_OPERATOR_CONFIG)


def get_project_config(name: str) -> ProjectConfig | None:
    """Look up a project config by name.

    Matches against the filename stem convention ("owner-repo") or
    the full name ("owner/repo").

    Returns None if no matching config is found.
    """
    from django.conf import settings

    configs = load_project_configs(settings.FRANK_PROJECTS_DIR)
    for config in configs:
        if f"{config.owner}-{config.repo}" == name or config.full_name == name:
            return config
    return None
