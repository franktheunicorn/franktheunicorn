"""Anti-pattern storage and Bayesian suppression model.

When the operator flags a draft comment as bad ("flagged-anti-pattern"),
we record that as an anti-pattern.  Future drafts that overlap with known
anti-patterns get a suppression penalty based on a simple Bayesian update:

    P(anti | phrase) ≈ count / total_shown

This is intentionally lightweight and local - no ML runtime, no model file.
The operator's feedback loop is the entire algorithm.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from franktheunicorn.models import AntiPattern

logger = logging.getLogger(__name__)


def _normalise(text: str) -> str:
    """Lower-case and collapse whitespace for fuzzy phrase matching."""
    return re.sub(r"\s+", " ", text.lower().strip())


def record_anti_pattern(
    session: Session,
    label: str,
    phrase: str,
    project_slug: str | None = None,
) -> AntiPattern:
    """Record (or update) an anti-pattern in the database.

    If an anti-pattern with the same label (and optional project_slug) already
    exists, its count and probability are updated via Bayesian update.
    """
    norm_phrase = _normalise(phrase)
    existing = (
        session.query(AntiPattern)
        .filter(
            AntiPattern.label == label,
            AntiPattern.project_slug == project_slug,
        )
        .first()
    )
    if existing:
        existing.count += 1
        existing.total_shown += 1
        existing.probability = existing.count / existing.total_shown
        existing.phrase = norm_phrase  # update to most recent phrasing
        logger.info("Updated anti-pattern %r p=%.2f", label, existing.probability)
        return existing

    ap = AntiPattern(
        label=label,
        phrase=norm_phrase,
        count=1,
        total_shown=1,
        probability=1.0,
        project_slug=project_slug,
    )
    session.add(ap)
    logger.info("Recorded new anti-pattern %r", label)
    return ap


def record_shown(
    session: Session,
    label: str,
    project_slug: str | None = None,
) -> None:
    """Increment total_shown for an anti-pattern without incrementing count.

    Call this each time we show a draft that matches a given pattern so the
    denominator stays accurate.
    """
    existing = (
        session.query(AntiPattern)
        .filter(
            AntiPattern.label == label,
            AntiPattern.project_slug == project_slug,
        )
        .first()
    )
    if existing:
        existing.total_shown += 1
        existing.probability = existing.count / existing.total_shown


def suppression_score(
    session: Session,
    draft_body: str,
    project_slug: str | None = None,
) -> float:
    """Return a suppression score in [0, 1] for a draft comment.

    A score of 1.0 means "definitely suppress", 0.0 means "no match found".
    Looks for phrase overlaps across all relevant anti-patterns.
    """
    norm_body = _normalise(draft_body)
    patterns: list[AntiPattern] = (
        session.query(AntiPattern)
        .filter((AntiPattern.project_slug == project_slug) | (AntiPattern.project_slug.is_(None)))
        .all()
    )

    if not patterns:
        return 0.0

    max_prob: float = 0.0
    for ap in patterns:
        # Simple substring match - cheap and deterministic.
        if ap.phrase and ap.phrase in norm_body:
            if ap.probability > max_prob:
                max_prob = ap.probability
                logger.debug("Anti-pattern match: %r p=%.2f", ap.label, ap.probability)

    return max_prob


def list_anti_patterns(
    session: Session,
    project_slug: str | None = None,
) -> list[AntiPattern]:
    """Return all anti-patterns, optionally filtered by project."""
    q = session.query(AntiPattern)
    if project_slug is not None:
        q = q.filter(AntiPattern.project_slug == project_slug)
    return q.order_by(AntiPattern.probability.desc()).all()
