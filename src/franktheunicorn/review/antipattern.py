"""
Anti-pattern detection and storage.

Anti-patterns are review comments or suggestion styles the operator doesn't
want to make. Built from rejected/edited drafts, they serve as a lightweight
feedback signal to improve future drafts.

This module provides a simple string-matching heuristic. A more sophisticated
Bayesian or probabilistic approach can replace it later without changing
the interface.
"""

from __future__ import annotations

from franktheunicorn.core.models import AntiPattern, Project


def check_against_anti_patterns(
    comment_body: str,
    project: Project | None = None,
) -> list[AntiPattern]:
    """
    Check a draft comment against known anti-patterns.

    Returns matching anti-patterns, ordered by weight.
    Matches are simple case-insensitive substring checks for now.
    """
    queryset = AntiPattern.objects.all()
    if project is not None:
        # Check both project-specific and global anti-patterns
        queryset = queryset.filter(project__in=[project, None])

    matches: list[AntiPattern] = []
    comment_lower = comment_body.lower()
    for ap in queryset:
        if ap.pattern_text.lower() in comment_lower:
            matches.append(ap)

    return matches


def record_anti_pattern(
    pattern_text: str,
    description: str = "",
    project: Project | None = None,
) -> AntiPattern:
    """Record a new anti-pattern from operator feedback."""
    ap, created = AntiPattern.objects.get_or_create(
        pattern_text=pattern_text,
        project=project,
        defaults={"description": description},
    )
    if not created:
        ap.times_triggered += 1
        ap.save(update_fields=["times_triggered"])
    return ap
