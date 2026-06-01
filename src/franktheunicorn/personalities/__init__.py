"""Reviewer agent personality loader.

Personalities are markdown files with ``## `` section headers that define
the agent's voice for different contexts (internal dashboard vs external
GitHub comments).  The default personality is "frank" (Frank the Unicorn).

Resolution order:
1. ``config/active/personalities/{name}.md`` (operator customisation)
2. Bundled ``src/franktheunicorn/personalities/{name}.md``
3. ``None`` (personality disabled — falls back to generic prompt text)
"""

from __future__ import annotations

import importlib.resources
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)

_USER_PERSONALITIES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent / "config" / "active" / "personalities"
)


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


def _read_personality_file(name: str) -> tuple[str, float] | None:
    """Locate and read a personality markdown file by name.

    Returns ``(text, mtime_or_0.0)``. Bundled (importlib) resources have no
    mtime so they're returned with 0.0 — they only change when the package
    is reinstalled, which restarts the process and clears the cache anyway.
    """
    # 1. Operator override in config/active/personalities/
    user_path = _USER_PERSONALITIES_DIR / f"{name}.md"
    if user_path.is_file():
        try:
            text = user_path.read_text(encoding="utf-8")
            mtime = user_path.stat().st_mtime
            return text, mtime
        except OSError:
            logger.debug("Failed to read user personality file: %s", user_path)

    # 2. Bundled with the package
    try:
        ref = importlib.resources.files(__package__).joinpath(f"{name}.md")
        return ref.read_text(encoding="utf-8"), 0.0
    except (FileNotFoundError, TypeError, OSError):
        return None


# mtime-aware cache: lets operators edit a personality file and have the
# change picked up on the next prompt build, instead of being stuck on the
# version loaded at process start.
_personality_cache: dict[str, tuple[float, Personality | None]] = {}


def load_personality(name: str) -> Personality | None:
    """Load a personality by name.  Returns ``None`` if *name* is empty or not found."""
    if not name:
        return None

    read = _read_personality_file(name)
    if read is None:
        cached = _personality_cache.get(name)
        if cached is not None:
            return cached[1]
        logger.warning("Personality '%s' not found.", name)
        _personality_cache[name] = (0.0, None)
        return None

    raw, mtime = read
    cached = _personality_cache.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    sections = _parse_sections(raw)
    personality = Personality(
        name=name,
        identity=sections.get("identity", ""),
        internal_voice=sections.get("internal voice", ""),
        external_voice=sections.get("external voice", ""),
        review_philosophy=sections.get("review philosophy", ""),
        raw=raw,
    )
    _personality_cache[name] = (mtime, personality)
    return personality


def clear_personality_cache() -> None:
    """Drop all cached personalities. Used by tests; safe to call at any time."""
    _personality_cache.clear()


__all__ = ["Personality", "clear_personality_cache", "load_personality"]
