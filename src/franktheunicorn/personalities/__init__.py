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
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_SUBSECTION_RE = re.compile(r"^### (.+)$", re.MULTILINE)

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
    # Verbatim review examples grouped by category: ((category, text), ...)
    examples: tuple[tuple[str, str], ...] = field(default_factory=tuple)


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


def _parse_review_examples(section_body: str) -> tuple[tuple[str, str], ...]:
    """Parse the ``## Review Examples`` section body into (category, text) pairs.

    The section uses ``### category`` sub-headers and blockquote-formatted
    comment bodies (lines starting with ``> ``).
    """
    examples: list[tuple[str, str]] = []
    sub_headers = list(_SUBSECTION_RE.finditer(section_body))
    for i, match in enumerate(sub_headers):
        category = match.group(1).strip().lower()
        start = match.end()
        end = sub_headers[i + 1].start() if i + 1 < len(sub_headers) else len(section_body)
        block = section_body[start:end].strip()

        # Extract blockquote lines ("> text" or ">").
        lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("> "):
                lines.append(line[2:])
            elif line.strip() == ">":
                lines.append("")

        text = "\n".join(lines).strip()
        if text:
            examples.append((category, text))

    return tuple(examples)


def _read_personality_file(name: str) -> str | None:
    """Locate and read a personality markdown file by name."""
    # 1. Operator override in config/active/personalities/
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
    examples = _parse_review_examples(sections.get("review examples", ""))

    return Personality(
        name=name,
        identity=sections.get("identity", ""),
        internal_voice=sections.get("internal voice", ""),
        external_voice=sections.get("external voice", ""),
        review_philosophy=sections.get("review philosophy", ""),
        raw=raw,
        examples=examples,
    )


def refresh_personality(name: str) -> None:
    """Clear the cached personality for *name* so the next load re-reads the file.

    Call this after rebuilding or editing a personality file to make the change
    take effect without restarting the worker.
    """
    load_personality.cache_clear()
    logger.debug("Cleared personality cache for '%s'", name)


__all__ = ["Personality", "load_personality", "refresh_personality"]
