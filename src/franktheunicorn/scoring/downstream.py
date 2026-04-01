"""Cross-project downstream impact detection (v1.5).

Detects when a PR touches APIs that downstream projects depend on.
Tracked APIs are stored as a JSON file per downstream project.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_WEIGHT = 20


def load_tracked_apis(tracked_apis_file: str) -> dict[str, list[str]]:
    """Load tracked API symbols from a JSON file.

    Expected format::

        {
            "files": ["path/to/api.py", "path/to/public_api.scala"],
            "symbols": ["DataFrame.mapInArrow", "SparkSession.builder"],
            "patterns": ["connector/connect/"]
        }

    Returns an empty dict if the file doesn't exist or is malformed.
    """
    path = Path(tracked_apis_file).expanduser()
    if not path.exists():
        logger.debug("Tracked APIs file not found: %s", path)
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("Tracked APIs file is not a JSON object: %s", path)
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read tracked APIs file: %s", path, exc_info=True)
        return {}


def score_downstream_impact(
    changed_files: list[str],
    diff_text: str,
    tracked_apis: dict[str, list[str]],
    weight: int = DEFAULT_WEIGHT,
) -> int | None:
    """Score a PR for downstream impact based on tracked APIs.

    Checks three categories:
    - ``files``: exact file path matches
    - ``patterns``: substring matches against file paths
    - ``symbols``: substring matches against the diff text

    Returns the weight if any match is found, else None.
    """
    if not tracked_apis:
        return None

    tracked_files = set(tracked_apis.get("files", []))
    patterns = tracked_apis.get("patterns", [])
    symbols = tracked_apis.get("symbols", [])

    # Check file-level matches.
    for f in changed_files:
        if f in tracked_files:
            logger.debug("Downstream impact: file match %s", f)
            return weight

    # Check pattern matches against file paths.
    for f in changed_files:
        for pattern in patterns:
            if pattern in f:
                logger.debug("Downstream impact: pattern '%s' matched file %s", pattern, f)
                return weight

    # Check symbol matches in the diff text.
    if diff_text and symbols:
        diff_lower = diff_text.lower()
        for symbol in symbols:
            if symbol.lower() in diff_lower:
                logger.debug("Downstream impact: symbol '%s' found in diff", symbol)
                return weight

    return None
