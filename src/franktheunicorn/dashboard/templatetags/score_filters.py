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
