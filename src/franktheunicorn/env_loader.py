"""Load secrets from a ``.env`` file into ``os.environ``.

Used by ``manage.py`` and the worker entry point so secrets such as
``ANTHROPIC_API_KEY`` and ``FRANK_GITHUB_TOKEN`` are available without
requiring the operator to ``export`` them manually before ``make serve``
or ``make worker``.

Existing environment variables always take precedence — values set by
the shell or Docker Compose's own ``.env`` substitution are never
overwritten.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Populate ``os.environ`` from ``path`` if it exists.

    Silently no-ops when the file is missing. Lines that don't parse as
    ``KEY=VALUE`` are skipped. Quotes around values are stripped. Keys
    already present in the environment are left untouched.
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_project_dotenv(start: Path | None = None) -> None:
    """Find and load the project's ``.env`` from ``start`` upward.

    Walks up from ``start`` (defaulting to this file's location) looking
    for a ``.env`` next to ``manage.py`` or ``pyproject.toml``.
    """
    here = (start or Path(__file__)).resolve()
    for parent in (here, *here.parents):
        if (parent / "manage.py").is_file() or (parent / "pyproject.toml").is_file():
            load_dotenv(parent / ".env")
            return
