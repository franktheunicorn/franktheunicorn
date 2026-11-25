from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import template
from django.template.defaultfilters import floatformat

register = template.Library()


@register.filter(name="score100")
def score100(value: object) -> str:
    """Render a stored 0-1 interest score on a friendlier 0-100 scale.

    Presentation only: the score is stored as a 0-1 float (e.g. 0.15) and this
    filter shows it multiplied by 100 (``"15"``) at render time. It never
    mutates the stored value and is not used for any comparison, threshold, or
    ordering. Non-numeric input renders as an empty string so a missing score
    degrades quietly instead of raising.
    """
    try:
        scaled = Decimal(str(value)) * 100
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return floatformat(scaled, 0)


@register.filter(name="get_item")
def get_item(mapping: object, key: object) -> object:
    """``{{ counts|get_item:"not-ready" }}`` — dict lookup for keys the dot syntax can't spell.

    Django's ``{{ d.not-ready }}`` is a subtraction, not a lookup. Returns
    ``None`` for anything that isn't a mapping or lacks the key, so
    ``|default:0`` behaves.
    """
    if isinstance(mapping, dict):
        return mapping.get(key)
    return None


@register.filter(name="reviewer")
def reviewer(value: object) -> str:
    """Render a reviewer login with its standing on the project.

    ``{{ r|reviewer }}`` where ``r`` is a ``scoring.reviewer_standing.Reviewer``:
    bold for an org member, bold with a trailing ``*`` for a committer, plain
    otherwise. A bare string renders plain, so a caller that has no standing
    to hand still shows the name.
    """
    from django.utils.html import format_html

    from franktheunicorn.scoring.reviewer_standing import Reviewer

    if isinstance(value, Reviewer):
        if value.is_committer:
            return format_html(
                '<strong class="reviewer reviewer-committer" '
                'title="{} is a committer on this project">{}</strong>*',
                value.login,
                value.login,
            )
        if value.is_member:
            return format_html(
                '<strong class="reviewer reviewer-member" '
                'title="{} is a member of the org">{}</strong>',
                value.login,
                value.login,
            )
        return format_html('<span class="reviewer">{}</span>', value.login)
    if value is None or value == "":
        return ""
    return format_html('<span class="reviewer">{}</span>', str(value))
