"""Build reviewer persona markdown from GitHub review comment history.

Given a set of classified review comments (from curator/classifier.py), this
module analyses patterns and synthesises a personality markdown file that can be
used by the ``personalities`` loader as a named agent persona.

If an LLM backend is provided the Identity/Voice/Philosophy sections are written
by the model.  Without a backend a deterministic template fallback is used.
The ``## Review Examples`` section is *always* built from verbatim comments —
it is never LLM-generated.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.curator.classifier import ClassifiedComment

logger = logging.getLogger(__name__)

# Minimum comment body length to qualify as a verbatim example.
MIN_EXAMPLE_LENGTH = 50

# Maximum verbatim examples to include per category.
MAX_EXAMPLES_PER_CATEGORY = 2

# Top-N categories to feature in the identity prose.
TOP_CATEGORIES_N = 5

_QUESTION_RE = re.compile(r"\?")
_SUGGESTION_RE = re.compile(
    r"(?i)(consider |you might|you could|try |perhaps|maybe |i'd suggest|"
    r"one option|an alternative)"
)


@dataclass
class PersonaStats:
    """Statistical summary of a reviewer's comment patterns."""

    username: str
    total_comments: int
    category_distribution: dict[str, float]  # {category: fraction 0-1}
    tone_flag_rate: float
    avg_comment_length: float
    question_rate: float
    suggestion_rate: float
    top_categories: list[str]  # ordered by descending frequency


def _compute_stats(
    username: str,
    classified: list[ClassifiedComment],
) -> PersonaStats:
    """Derive statistics from a set of classified review comments."""
    total = len(classified)
    if total == 0:
        return PersonaStats(
            username=username,
            total_comments=0,
            category_distribution={},
            tone_flag_rate=0.0,
            avg_comment_length=0.0,
            question_rate=0.0,
            suggestion_rate=0.0,
            top_categories=[],
        )

    category_counts: dict[str, int] = {}
    tone_flagged = 0
    total_length = 0
    questions = 0
    suggestions = 0

    for cc in classified:
        category_counts[cc.category] = category_counts.get(cc.category, 0) + 1
        if cc.tone_flagged:
            tone_flagged += 1
        body = cc.raw.body
        total_length += len(body)
        if _QUESTION_RE.search(body):
            questions += 1
        if _SUGGESTION_RE.search(body):
            suggestions += 1

    top_categories = sorted(category_counts, key=lambda c: -category_counts[c])[:TOP_CATEGORIES_N]
    category_distribution = {cat: category_counts[cat] / total for cat in top_categories}

    return PersonaStats(
        username=username,
        total_comments=total,
        category_distribution=category_distribution,
        tone_flag_rate=tone_flagged / total,
        avg_comment_length=total_length / total,
        question_rate=questions / total,
        suggestion_rate=suggestions / total,
        top_categories=top_categories,
    )


def _select_examples(
    classified: list[ClassifiedComment],
    max_per_category: int = MAX_EXAMPLES_PER_CATEGORY,
) -> dict[str, list[str]]:
    """Select representative verbatim examples per category.

    Selection criteria:
    - Comment body length > ``MIN_EXAMPLE_LENGTH``
    - No tone flags (clean comments only)
    - Up to ``max_per_category`` per category, preferring longer comments
    """
    by_category: dict[str, list[ClassifiedComment]] = {}
    for cc in classified:
        if not cc.tone_flagged and len(cc.raw.body.strip()) > MIN_EXAMPLE_LENGTH:
            by_category.setdefault(cc.category, []).append(cc)

    examples: dict[str, list[str]] = {}
    for cat, ccs in by_category.items():
        sorted_ccs = sorted(ccs, key=lambda c: -len(c.raw.body))
        examples[cat] = [cc.raw.body.strip() for cc in sorted_ccs[:max_per_category]]

    return examples


def _format_review_examples(examples_by_category: dict[str, list[str]]) -> str:
    """Format the ``## Review Examples`` persona section."""
    if not examples_by_category:
        return ""

    lines: list[str] = ["## Review Examples", ""]
    for cat, texts in examples_by_category.items():
        lines.append(f"### {cat}")
        for text in texts:
            # Render each line of the comment body as a markdown blockquote.
            quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in text.split("\n"))
            lines.append(quoted)
            lines.append("")

    return "\n".join(lines).rstrip()


def _build_fallback_persona(
    stats: PersonaStats,
    examples_by_category: dict[str, list[str]],
) -> str:
    """Generate a template-based persona when no LLM is available.

    Returns Identity/Voice/Philosophy sections followed by the Review Examples
    section (if any examples were selected).
    """
    top = stats.top_categories

    style_notes: list[str] = []
    if stats.question_rate > 0.3:
        style_notes.append("asks clarifying questions frequently")
    if stats.suggestion_rate > 0.4:
        style_notes.append("prefers offering alternatives over stating problems")
    if stats.avg_comment_length > 200:
        style_notes.append("writes detailed, thorough comments")
    elif stats.avg_comment_length < 80:
        style_notes.append("writes concise, direct comments")
    style_desc = ", ".join(style_notes) if style_notes else "writes focused review comments"

    focus_str = (
        f"{', '.join(top[:2])}" if len(top) >= 2 else (top[0] if top else "general code quality")
    )

    sections: list[str] = [
        f"# {stats.username}",
        "",
        "## Identity",
        (
            f"You are {stats.username}, a code reviewer with a history of "
            f"{stats.total_comments} review comments across open-source projects. "
            f"Your review history focuses most heavily on {focus_str}."
        ),
        "",
        "## Internal Voice",
        (
            f"Direct and technically precise. You {style_desc}. "
            "When reviewing, focus on what matters most and skip trivial nits when there "
            "are substantive issues to address."
        ),
        "",
        "## External Voice",
        (
            "Write as a professional, constructive code reviewer. Be technically precise "
            "and actionable. Suggest fixes, not just problems. Include file paths and line "
            "references where relevant."
        ),
        "",
        "## Review Philosophy",
    ]

    if top:
        sections.append(f"- Focus areas: {', '.join(top[:3])}")
    cat_bullets = "\n".join(
        f"- {cat}: {stats.category_distribution.get(cat, 0):.0%}" for cat in top[:3]
    )
    if cat_bullets:
        sections.append(f"- Category distribution:\n{cat_bullets}")
    if stats.question_rate > 0.2:
        sections.append("- Ask clarifying questions when intent is unclear")
    if stats.suggestion_rate > 0.3:
        sections.append("- Suggest concrete alternative implementations")

    examples_section = _format_review_examples(examples_by_category)
    if examples_section:
        sections.append("")
        sections.append(examples_section)

    return "\n".join(sections)


def _build_llm_persona(
    stats: PersonaStats,
    examples_by_category: dict[str, list[str]],
    backend_config: LLMBackendConfig,
) -> str | None:
    """Use an LLM to synthesise a richer persona narrative.

    Returns the generated markdown (Identity through Review Philosophy only),
    or ``None`` if the LLM call fails or no API key is available.
    """
    import os

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not available; using fallback persona builder")
        return None

    api_key = os.environ.get(backend_config.api_key_env or "ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.debug("No API key for persona LLM synthesis; using fallback")
        return None

    cat_lines = "\n".join(
        f"  - {cat}: {pct:.0%}" for cat, pct in list(stats.category_distribution.items())[:5]
    )
    example_lines: list[str] = []
    for cat, texts in list(examples_by_category.items())[:3]:
        example_lines.append(f"  [{cat}]")
        for t in texts[:1]:
            snippet = t[:300] + "..." if len(t) > 300 else t
            example_lines.append(f'    "{snippet}"')
    examples_text = "\n".join(example_lines) if example_lines else "  (no examples available)"

    style_hints: list[str] = []
    if stats.question_rate > 0.3:
        style_hints.append("asks clarifying questions frequently")
    if stats.suggestion_rate > 0.4:
        style_hints.append("prefers offering alternatives rather than just stating problems")
    if stats.avg_comment_length > 200:
        style_hints.append("writes detailed, multi-sentence comments")
    elif stats.avg_comment_length < 80:
        style_hints.append("writes concise, targeted comments")
    style_desc = "; ".join(style_hints) or "writes focused comments"

    prompt = (
        f"You are helping build a reviewer persona profile for use as an LLM agent "
        f"system prompt.\n\n"
        f"Reviewer: {stats.username}\n"
        f"Total comments analysed: {stats.total_comments}\n"
        f"Category distribution:\n{cat_lines}\n"
        f"Style: {style_desc}\n"
        f"Tone flag rate: {stats.tone_flag_rate:.0%} (low is good)\n"
        f"Representative examples of their actual comments:\n{examples_text}\n\n"
        f"Write a persona profile in markdown with exactly these four sections "
        f"(using ## headers):\n\n"
        f"## Identity\n"
        f"A 3-4 sentence description of who this reviewer is and what they focus on, "
        f'written as "You are {stats.username}...". '
        f"Base the focus areas on the category distribution above.\n\n"
        f"## Internal Voice\n"
        f"2-3 sentences about this reviewer's characteristic style for internal/dashboard "
        f"use. Reference how they phrase things, their questioning vs direct style, and "
        f"comment depth.\n\n"
        f"## External Voice\n"
        f"2-3 sentences about how they write public review comments. Should be professional "
        f"and constructive. Reference specific stylistic patterns from the examples.\n\n"
        f"## Review Philosophy\n"
        f"4-6 bullet points capturing their core reviewing principles, derived from the "
        f"category distribution and examples.\n\n"
        f"Keep each section concise. Do not include the ## Review Examples section — "
        f"that will be appended separately. Do not add any other sections. "
        f"Output only the markdown, no preamble."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=backend_config.model or "claude-haiku-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        generated = response.content[0].text.strip()  # type: ignore[union-attr]
        if "## Identity" not in generated:
            logger.warning("LLM persona response missing ## Identity section; using fallback")
            return None
        return generated
    except Exception:
        logger.warning("LLM persona synthesis failed; using fallback", exc_info=True)
        return None


def build_persona_from_comments(
    username: str,
    classified_comments: list[ClassifiedComment],
    backend_config: LLMBackendConfig | None = None,
) -> str:
    """Build persona markdown from classified review comments.

    Analyses comment patterns, selects representative examples, and generates
    a personality file in the format expected by ``personalities/__init__.py``.

    If ``backend_config`` points to a real LLM the Identity/Voice/Philosophy
    sections are written by the model; otherwise a deterministic template is
    used.  The ``## Review Examples`` section is always built from verbatim
    comments.

    Returns the raw markdown content suitable for writing to a ``.md`` file.
    """
    stats = _compute_stats(username, classified_comments)
    examples_by_category = _select_examples(classified_comments)

    persona_body: str | None = None
    if backend_config is not None and backend_config.provider not in ("stub", ""):
        persona_body = _build_llm_persona(stats, examples_by_category, backend_config)

    if persona_body is None:
        # Fallback path: _build_fallback_persona includes the examples section.
        return _build_fallback_persona(stats, examples_by_category)

    # LLM path: append the verbatim examples section after the generated prose.
    examples_section = _format_review_examples(examples_by_category)
    result = f"# {username}\n\n{persona_body}"
    if examples_section:
        result = result.rstrip() + "\n\n" + examples_section
    return result


__all__ = [
    "PersonaStats",
    "build_persona_from_comments",
]
