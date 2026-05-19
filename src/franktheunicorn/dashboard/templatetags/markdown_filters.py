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


@register.filter(name="render_markdown_inline", is_safe=True)
def render_markdown_inline(value: str | None) -> SafeString:
    """Render markdown for inline contexts (titles, link text).

    Strips the wrapping <p>...</p> that render_markdown emits so the
    result can be placed inside an <a>, <h2>, etc. without block-level
    elements nesting illegally inside inline elements.
    """
    if not value:
        return mark_safe("")
    rendered: str = _md.renderInline(str(value))
    return mark_safe(rendered)
