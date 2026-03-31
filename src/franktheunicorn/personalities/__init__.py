"""Reviewer agent personality loader.

Personalities are markdown files with ``## `` section headers that define
the agent's voice for different contexts (internal dashboard vs external
GitHub comments).  The default personality is "frank" (Frank the Unicorn).

Resolution order:
1. ``~/.review-agent/personalities/{name}.md`` (operator customisation)
2. Bundled ``src/franktheunicorn/personalities/{name}.md``
3. ``None`` (personality disabled — falls back to generic prompt text)
"""

from __future__ import annotations

import importlib.resources
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)

_USER_PERSONALITIES_DIR = Path.home() / ".review-agent" / "personalities"


@dataclass(frozen=True)
class Personality:
    """Parsed personality with per-context voice sections."""

    name: str
    identity: str
    internal_voice: str
    external_voice: str
    review_philosophy: str
    raw: str


def _parse_sections(raw: str) -> dict[str, str]:
    """Split markdown into ``{header_lower: body}`` by ``## `` headers."""
    sections: dict[str, str] = {}
    headers = list(_SECTION_RE.finditer(raw))
    for i, match in enumerate(headers):
        key = match.group(1).strip().lower()
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        sections[key] = raw[start:end].strip()
    return sections


def _read_personality_file(name: str) -> str | None:
    """Locate and read a personality markdown file by name."""
    # 1. Operator override in ~/.review-agent/personalities/
    user_path = _USER_PERSONALITIES_DIR / f"{name}.md"
    if user_path.is_file():
        try:
            return user_path.read_text(encoding="utf-8")
        except OSError:
            logger.debug("Failed to read user personality file: %s", user_path)

    # 2. Bundled with the package
    try:
        ref = importlib.resources.files(__package__).joinpath(f"{name}.md")
        return ref.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError, OSError):
        return None


@lru_cache(maxsize=4)
def load_personality(name: str) -> Personality | None:
    """Load a personality by name.  Returns ``None`` if *name* is empty or not found."""
    if not name:
        return None

    raw = _read_personality_file(name)
    if raw is None:
        logger.warning("Personality '%s' not found.", name)
        return None

    sections = _parse_sections(raw)
    return Personality(
        name=name,
        identity=sections.get("identity", ""),
        internal_voice=sections.get("internal voice", ""),
        external_voice=sections.get("external voice", ""),
        review_philosophy=sections.get("review philosophy", ""),
        raw=raw,
    )


__all__ = ["Personality", "load_personality"]
