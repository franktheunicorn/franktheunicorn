"""Load operator and project configs from YAML files on disk.

String values support ``${VAR_NAME}`` env-var expansion.  After
``yaml.safe_load()`` the parsed data is walked recursively and every
``${…}`` reference is replaced with ``os.environ.get(name, "")``.
Partial substitution works too (e.g. ``"${HOME}/frank-data"``).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from django.conf import settings
from pydantic import ValidationError

from franktheunicorn.config.models import OperatorConfig, ProjectConfig

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(data: Any) -> Any:
    """Recursively expand ``${VAR}`` patterns in string values."""
    if isinstance(data, str):
        return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), data)
    if isinstance(data, dict):
        return {k: _expand_env_vars(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_expand_env_vars(item) for item in data]
    return data


def _normalize_project_config(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy/alias project config keys before model parsing."""
    merge_queue = data.get("merge_queue")
    if isinstance(merge_queue, dict):
        mq = dict(merge_queue)
        if "restack" in mq and "restack_enabled" not in mq:
            mq["restack_enabled"] = mq["restack"]
        if mq.get("stale_migration_strategy") == "none" and "delete_stale_migrations" not in mq:
            mq["delete_stale_migrations"] = False
        if (
            mq.get("stale_migration_strategy") == "app-local-diff"
            and "delete_stale_migrations" not in mq
        ):
            mq["delete_stale_migrations"] = True
        data = {**data, "merge_queue": mq}
    return data


def load_operator_config(path: str | Path) -> OperatorConfig:
    """Load operator config from a YAML file. Returns defaults if file doesn't exist."""
    p = Path(path)
    try:
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data = _expand_env_vars(data)
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
            data = _expand_env_vars(data)
            data = _normalize_project_config(data)
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
