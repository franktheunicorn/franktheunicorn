from __future__ import annotations

from django import template
from django.utils.safestring import SafeString, mark_safe
from markdown_it import MarkdownIt

register = template.Library()

_md = MarkdownIt("gfm-like", {"html": False})

# Title renderer: formatting only (code spans, emphasis), NO links. PR titles
# are attacker-controlled and get rendered inside the dashboard's own <a>
# elements — markdown links or linkified bare URLs would nest anchors and let
# a PR author control where clicking the title navigates.
_md_title = MarkdownIt("zero", {"html": False, "linkify": False}).enable(
    ["backticks", "emphasis", "strikethrough", "escape", "entity"]
)


@register.filter(name="render_markdown", is_safe=True)
def render_markdown(value: str | None) -> SafeString:
    if not value:
        return mark_safe("")
    rendered: str = _md.render(str(value))
    return mark_safe(rendered)


@register.filter(name="render_markdown_inline", is_safe=True)
def render_markdown_inline(value: str | None) -> SafeString:
    """Render markdown for inline contexts (titles, link text).

    Uses the link-free title renderer: the call sites put the result inside
    anchors/headings, where author-controlled links must not appear.
    """
    if not value:
        return mark_safe("")
    rendered: str = _md_title.renderInline(str(value))
    return mark_safe(rendered)
