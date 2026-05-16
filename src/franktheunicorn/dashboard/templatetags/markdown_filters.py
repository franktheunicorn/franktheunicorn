from __future__ import annotations

from django import template
from django.utils.safestring import SafeString, mark_safe
from markdown_it import MarkdownIt

register = template.Library()

_md = MarkdownIt("gfm-like", {"html": False})


@register.filter(name="render_markdown", is_safe=True)
def render_markdown(value: str | None) -> SafeString:
    if not value:
        return mark_safe("")
    rendered: str = _md.render(str(value))
    return mark_safe(rendered)
