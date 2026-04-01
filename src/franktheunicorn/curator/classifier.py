"""Classify review comments by category and flag tone issues."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from franktheunicorn.curator.scraper import RawComment

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)

CATEGORIES = [
    "correctness",
    "style",
    "architectural",
    "test-coverage",
    "naming",
    "security",
    "moderation",
    "other",
]

TONE_FLAGS = ["abrasive", "snarky", "pedantic", "condescending"]

# Keywords used by the fallback classifier (no LLM needed).
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "correctness": [
        "bug",
        "error",
        "wrong",
        "incorrect",
        "fix",
        "broken",
        "crash",
        "null",
        "none",
        "exception",
        "fail",
        "off-by-one",
        "race condition",
    ],
    "style": [
        "style",
        "format",
        "indent",
        "whitespace",
        "naming convention",
        "pep8",
        "ruff",
        "lint",
        "readability",
        "clean up",
    ],
    "architectural": [
        "architecture",
        "design",
        "pattern",
        "coupling",
        "abstraction",
        "refactor",
        "module",
        "layer",
        "separation",
        "dependency",
    ],
    "test-coverage": [
        "test",
        "coverage",
        "assert",
        "mock",
        "fixture",
        "spec",
        "unit test",
        "integration test",
    ],
    "naming": [
        "name",
        "naming",
        "rename",
        "variable name",
        "descriptive",
        "misleading name",
    ],
    "security": [
        "security",
        "vulnerability",
        "injection",
        "xss",
        "csrf",
        "auth",
        "permission",
        "sanitize",
        "escape",
        "secret",
        "token",
    ],
    "moderation": [
        "inappropriate",
        "offensive",
        "language",
        "tone",
        "respectful",
    ],
}

_TONE_KEYWORDS: dict[str, list[str]] = {
    "abrasive": [
        "terrible",
        "awful",
        "ridiculous",
        "stupid",
        "never do this",
        "obviously wrong",
    ],
    "snarky": [
        "clearly",
        "obviously",
        "did you even",
        "you should know",
        "as anyone would know",
    ],
    "pedantic": [
        "actually",
        "technically",
        "to be precise",
        "strictly speaking",
        "per the spec",
    ],
    "condescending": [
        "junior",
        "beginner",
        "basic",
        "trivial",
        "simple mistake",
        "you need to learn",
    ],
}


@dataclass
class ClassifiedComment:
    """A comment with classification metadata."""

    raw: RawComment
    category: str
    tone_flagged: bool
    tone_flags: list[str] = field(default_factory=list)


def classify_comments(
    comments: list[RawComment],
    backend_config: LLMBackendConfig | None = None,
) -> list[ClassifiedComment]:
    """Classify comments using the configured LLM backend.

    Falls back to keyword-based classification when no LLM is available.
    """
    if backend_config is not None and backend_config.provider != "stub":
        return _classify_with_llm(comments, backend_config)
    return _classify_with_keywords(comments)


def _classify_with_keywords(
    comments: list[RawComment],
) -> list[ClassifiedComment]:
    """Keyword-based fallback classifier."""
    results: list[ClassifiedComment] = []
    for comment in comments:
        category = _keyword_category(comment.body)
        tone_flags = _keyword_tone_flags(comment.body)
        results.append(
            ClassifiedComment(
                raw=comment,
                category=category,
                tone_flagged=len(tone_flags) > 0,
                tone_flags=tone_flags,
            )
        )
    return results


def _keyword_category(body: str) -> str:
    """Match a comment body to a category via keyword scoring."""
    lower = body.lower()
    best_category = "other"
    best_score = 0
    for category, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in lower)
        if score > best_score:
            best_score = score
            best_category = category
    return best_category


def _keyword_tone_flags(body: str) -> list[str]:
    """Detect tone issues via keyword matching."""
    lower = body.lower()
    flags: list[str] = []
    for flag, keywords in _TONE_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            flags.append(flag)
    return flags


def _classify_with_llm(
    comments: list[RawComment],
    backend_config: LLMBackendConfig,
) -> list[ClassifiedComment]:
    """Classify comments using an LLM backend.

    Sends comments in batches and parses structured JSON responses.
    Falls back to keyword classification for any comments that fail.
    """
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not available, falling back to keywords")
        return _classify_with_keywords(comments)

    import os

    api_key = os.environ.get(backend_config.api_key_env or "ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("No API key found, falling back to keyword classifier")
        return _classify_with_keywords(comments)

    client = anthropic.Anthropic(api_key=api_key)
    results: list[ClassifiedComment] = []

    # Process in batches of 10
    batch_size = 10
    for i in range(0, len(comments), batch_size):
        batch = comments[i : i + batch_size]
        batch_text = "\n---\n".join(f"Comment {j}: {c.body}" for j, c in enumerate(batch))

        prompt = (
            "Classify each review comment below. For each comment, return a JSON "
            "object with keys: index (int), category (one of: "
            f"{', '.join(CATEGORIES)}), tone_flags (list of flags from: "
            f"{', '.join(TONE_FLAGS)}, or empty list).\n"
            "Return a JSON array of these objects.\n\n"
            f"{batch_text}"
        )

        try:
            response = client.messages.create(
                model=backend_config.model or "claude-sonnet-4-20250514",
                max_tokens=backend_config.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text  # type: ignore[union-attr]
            classifications = json.loads(raw_text)

            for item in classifications:
                idx = item.get("index", 0)
                if 0 <= idx < len(batch):
                    cat = item.get("category", "other")
                    if cat not in CATEGORIES:
                        cat = "other"
                    flags = [f for f in item.get("tone_flags", []) if f in TONE_FLAGS]
                    results.append(
                        ClassifiedComment(
                            raw=batch[idx],
                            category=cat,
                            tone_flagged=len(flags) > 0,
                            tone_flags=flags,
                        )
                    )
                    batch[idx] = None  # type: ignore[call-overload]
        except Exception:
            logger.exception("LLM classification failed for batch, using keywords")

        # Fall back for any unclassified comments in this batch
        for comment in batch:
            if comment is not None:
                category = _keyword_category(comment.body)
                tone_flags = _keyword_tone_flags(comment.body)
                results.append(
                    ClassifiedComment(
                        raw=comment,
                        category=category,
                        tone_flagged=len(tone_flags) > 0,
                        tone_flags=tone_flags,
                    )
                )

    return results
