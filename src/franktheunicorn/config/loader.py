"""Load operator and project configs from YAML files on disk."""

from __future__ import annotations

from pathlib import Path

import yaml

from franktheunicorn.config.models import OperatorConfig, ProjectConfig


def load_operator_config(path: str | Path) -> OperatorConfig:
    """Load operator config from a YAML file. Returns defaults if file doesn't exist."""
    p = Path(path)
    if not p.exists():
        return OperatorConfig()
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    return OperatorConfig(**data)


def load_project_configs(directory: str | Path) -> list[ProjectConfig]:
    """Load all project configs from YAML files in a directory."""
    d = Path(directory)
    if not d.exists():
        return []
    configs: list[ProjectConfig] = []
    for yaml_file in sorted(d.glob("*.yaml")):
        with yaml_file.open() as f:
            data = yaml.safe_load(f) or {}
        configs.append(ProjectConfig(**data))
    for yml_file in sorted(d.glob("*.yml")):
        with yml_file.open() as f:
            data = yaml.safe_load(f) or {}
        configs.append(ProjectConfig(**data))
    return configs
