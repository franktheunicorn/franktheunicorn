"""YAML schema validation constants and standalone config validator."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

# Valid GitHub owner/repo: alphanumeric, hyphens, dots, underscores.
# Must start and end with alphanumeric.
GITHUB_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$")

KNOWN_GOVERNANCE_VALUES: frozenset[str] = frozenset(
    {"standard", "asf", "personal", "corporate"}
)


def validate_yaml_file(
    path: str | Path,
    config_type: Literal["operator", "project"],
) -> list[str]:
    """Validate a YAML config file without loading it into the system.

    Returns a list of human-readable error strings. An empty list means valid.
    """
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig

    p = Path(path)
    try:
        with p.open() as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return [f"File not found: {p}"]
    except yaml.YAMLError as exc:
        return [f"Invalid YAML: {exc}"]

    if data is None:
        data = {}

    if not isinstance(data, dict):
        return [f"Expected a YAML mapping, got {type(data).__name__}"]

    model_class = OperatorConfig if config_type == "operator" else ProjectConfig
    try:
        model_class(**data)
    except ValidationError as exc:
        return [err["msg"] for err in exc.errors()]

    return []
