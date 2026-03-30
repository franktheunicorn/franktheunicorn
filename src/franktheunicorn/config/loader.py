"""Load operator and project configs from YAML files on disk."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from django.conf import settings
from pydantic import ValidationError

from franktheunicorn.config.models import OperatorConfig, ProjectConfig

logger = logging.getLogger(__name__)


def load_operator_config(path: str | Path) -> OperatorConfig:
    """Load operator config from a YAML file. Returns defaults if file doesn't exist."""
    p = Path(path)
    try:
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return OperatorConfig(**data)
    except FileNotFoundError:
        logger.debug("Operator config not found at %s, using defaults", p)
        return OperatorConfig()
    except yaml.YAMLError:
        logger.exception("Invalid YAML in operator config: %s", p)
        return OperatorConfig()
    except ValidationError:
        logger.exception("Validation error in operator config: %s", p)
        return OperatorConfig()


def load_project_configs(directory: str | Path) -> list[ProjectConfig]:
    """Load all project configs from YAML files in a directory."""
    d = Path(directory)
    if not d.is_dir():
        return []
    configs: list[ProjectConfig] = []
    for yaml_file in sorted(f for f in d.iterdir() if f.suffix in {".yaml", ".yml"}):
        try:
            with yaml_file.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            configs.append(ProjectConfig(**data))
        except yaml.YAMLError:
            logger.exception("Invalid YAML in project config: %s", yaml_file)
        except ValidationError:
            logger.exception("Validation error in project config: %s", yaml_file)
    return configs


def get_operator_config() -> OperatorConfig:
    """Load operator config from the path configured in Django settings."""
    return load_operator_config(settings.FRANK_OPERATOR_CONFIG)


def get_project_config(name: str) -> ProjectConfig | None:
    """Look up a project config by name.

    Matches against the filename stem convention ("owner-repo") or
    the full name ("owner/repo").

    Returns None if no matching config is found.
    """
    configs = load_project_configs(settings.FRANK_PROJECTS_DIR)
    for config in configs:
        if name in (f"{config.owner}-{config.repo}", config.full_name):
            return config
    return None
